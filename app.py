"""
Skurra — local disk scanner.
Run with:  python3 app.py
Stop with: close the window (or Control + C in the Terminal)

Opens in its own window when pywebview is installed (pip3 install pywebview),
otherwise falls back to your browser. Force the browser with SKURRA_BROWSER=1.
"""

import datetime
import hashlib
import json
import os
from collections import deque
import plistlib
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------- settings

HOME = Path.home()
PORT = 8765

# Bump this every time you ship a new build. Numbers only, dots between.
VERSION = "1.22.20"

# Where Skurra looks for news of a newer version: a small JSON file like
#   {"version": "1.1.0", "url": "https://.../Skurra.dmg", "notes": "What changed"}
# Point this at your own GitHub once you have one.
# A packaged app carries no trusted certificates of its own, so every https
# call fails with CERTIFICATE_VERIFY_FAILED unless we hand it a bundle.
try:
    import certifi
    HTTPS = ssl.create_default_context(cafile=certifi.where())
except Exception:
    HTTPS = ssl.create_default_context()

# The files an update comes from. Fixed here on purpose: latest.json says
# which version is out, it never gets to say where Skurra downloads from.
DOWNLOAD = {
    "mac": "https://github.com/nafe02/Skurra/releases/download/{tag}/Skurra.dmg",
    "windows": "https://github.com/nafe02/Skurra/releases/download/{tag}/Skurra.exe",
}

UPDATE_URL = os.environ.get("SKURRA_UPDATE_URL") or \
    "https://raw.githubusercontent.com/nafe02/Skurra/main/latest.json"

WINDOWS = sys.platform == "win32"

# Where index.html lives: next to this file, or inside the packaged app.
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

MIN_MB = 20          # ignore anything smaller than this
MIN_DAYS = 60        # ignore anything used more recently than this
ITEM_LIMIT = 45      # seconds any single folder may take before we move on

CACHE_INSIDE_APPS = ["Cache", "Code Cache", "GPUCache"]

if WINDOWS:
    LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
    ROAMING = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
    APPDATA_DIRS = [LOCAL, ROAMING]         # app data, one level in
    CACHES_DIR = LOCAL / "Temp"             # throwaway files
    BROWSER_CACHES = [                      # browsers bury theirs deeper
        LOCAL / "Google/Chrome/User Data/Default/Cache",
        LOCAL / "Microsoft/Edge/User Data/Default/Cache",
        LOCAL / "BraveSoftware/Brave-Browser/User Data/Default/Cache",
    ]
    APP_FOLDERS = []                        # apps come from the registry instead
    DATA = LOCAL / "Skurra"
    TRASH = None                            # the Recycle Bin, asked via Windows
else:
    APPDATA_DIRS = [HOME / "Library/Application Support"]
    CACHES_DIR = HOME / "Library/Caches"
    BROWSER_CACHES = []
    APP_FOLDERS = [Path("/Applications"), HOME / "Applications"]   # Apple's own are skipped
    DATA = HOME / "Library/Application Support/Skurra"
    TRASH = HOME / ".Trash"

# Logs an app wrote and never read again, and installers you already used.
if WINDOWS:
    # Not Temp: that is already the Caches folder on Windows, and listing it
    # here as well put every file in two drawers and counted it twice.
    LOG_DIRS = [LOCAL / "CrashDumps", ROAMING / "Microsoft/Windows/WER"]
else:
    LOG_DIRS = [HOME / "Library/Logs"]

INSTALLER_EXT = {".dmg", ".pkg", ".msi", ".exe", ".iso"}
INSTALLER_DAYS = 14          # old enough that you have surely installed it

FOLDERS = APPDATA_DIRS + [CACHES_DIR]

# ---------------------------------------------------------------- disk view

# The places worth looking through. Everything here belongs to the user.
def allowed_folder(path):
    """Did the person allow this one?

    Unknown is not a yes. On a Mac, looking inside Desktop or Downloads is
    what makes the system question appear, and an older grant does not
    survive an update. On Windows the yes is Skurra's own, and it stays.
    """
    state = load_perms()
    key = str(path)
    if state["folders"].get(key) != "granted":
        return False
    if WINDOWS:
        return True
    return state["grant_versions"].get(key) == VERSION


def disk_roots():
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music"]
    if WINDOWS:
        names = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
        # The real Desktop or Pictures can live under OneDrive, not beside
        # the home folder. Ask Windows where each one actually is.
        return [HOME] + [windows_known(n) for n in names]
    # Do not stat these. On a Mac, even asking "is Desktop there?" can
    # raise the system question and leave OK sitting on screen.
    return [HOME] + [HOME / n for n in names]


def windows_known(name):
    """The folder Windows itself calls Desktop, Pictures, and so on."""
    fallback = HOME / name
    ids = {
        "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
        "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
        "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
        "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
        "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
        "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    }
    text = ids.get(name)
    if not text:
        return fallback
    try:
        import ctypes
        parts = text.split("-")
        tail = bytes.fromhex(parts[3] + parts[4])

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong),
                        ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        guid = GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                    (ctypes.c_ubyte * 8)(*tail))
        ptr = ctypes.c_void_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return fallback
        found = Path(ctypes.wstring_at(ptr.value))
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        return found
    except Exception:
        return fallback

# Why Skurra asks for each of these. True of what it actually does: Downloads
# feeds the Installation files list, the rest feed the Disk space view.
WHY_FOLDER = {
    "Downloads": "Installers you have already used collect here. Skurra lists them so you can bin them.",
    "Desktop": "Measures what has piled up on your Desktop and been forgotten.",
    "Documents": "Measures large folders. Files are counted, never opened.",
    "Pictures": "Measures screenshots and exports. Your Photos library is always skipped.",
    "Movies": "Finds big video files, usually the largest thing on a Mac.",
    "Music": "Measures downloaded audio and leftovers from music apps.",
    "Videos": "Finds big video files, usually the largest thing on a PC.",
}

KINDS = {
    "video":    {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".webm", ".mpg", ".mpeg"},
    "audio":    {".mp3", ".m4a", ".wav", ".aac", ".flac", ".aiff", ".ogg", ".wma"},
    "image":    {".jpg", ".jpeg", ".png", ".gif", ".heic", ".tiff", ".tif", ".bmp",
                 ".raw", ".cr2", ".nef", ".webp", ".svg", ".psd", ".ai"},
    "document": {".pdf", ".doc", ".docx", ".pages", ".txt", ".rtf", ".xls", ".xlsx",
                 ".numbers", ".ppt", ".pptx", ".key", ".csv", ".md", ".epub"},
    "archive":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".dmg", ".iso", ".pkg", ".exe", ".msi"},
    "code":     {".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".go",
                 ".rs", ".rb", ".php", ".json", ".xml", ".sh", ".swift"},
}
KIND_OF = {ext: kind for kind, exts in KINDS.items() for ext in exts}

# Opening these makes macOS ask the user for Photos or Media permission, and
# nothing inside them is ours to tidy: Photos and Music own their own storage.
LIBRARY_BUNDLES = (".photoslibrary", ".photolibrary", ".aplibrary", ".migratedphotolibrary",
                   ".musiclibrary", ".tvlibrary", ".imovielibrary", ".theater", ".fcpbundle")


def is_library(name):
    return str(name).lower().endswith(LIBRARY_BUNDLES)


# Your own folders only. ~/Library (or AppData) holds app territory: it is huge,
# parts of it are guarded by macOS, and the Clean up tab already covers it.
NOT_YOURS = {"library", "appdata", "applications", "public"}

DISK_BUDGET = 30        # seconds for one folder, all of it
DISK_ITEM = 6           # seconds any single item inside may take


def kind_of(path, is_dir):
    if is_dir:
        return "folder"
    return KIND_OF.get(Path(path).suffix.lower(), "other")

# Where Skurra keeps its own notes. Never inside the app bundle.
DATA.mkdir(parents=True, exist_ok=True)
for _old in (Path(__file__).parent / "history.csv", Path(__file__).parent / "moved.log"):
    if _old.exists() and not (DATA / _old.name).exists():
        shutil.move(str(_old), str(DATA / _old.name))

# When packaged, Windows and macOS unpack the app into a temp folder called
# _MEIxxxxx. Those are the files Skurra is running from: never offer them.
OWN = {str(HERE).lower(), str(DATA).lower()}

# Proof that this build actually got as far as running. An update is only a
# success once the new copy writes this; a process that merely exists is not
# enough, because a bootloader sitting on a "Failed to load Python DLL" box
# counts as alive right up until someone clicks OK.
STARTED_FILE = DATA / "started.json"


def say_started():
    try:
        STARTED_FILE.write_text(json.dumps({"version": VERSION, "at": time.time()}))
    except OSError:
        pass

def is_ours(path):
    low = str(path).lower()
    if any(low == o or low.startswith(o + os.sep) for o in OWN):
        return True
    return Path(path).name.lower().startswith("_mei")


def is_our_app(app):
    """True for Skurra's own bundle, so it never offers to bin itself.

    is_ours() asks "is this path inside Skurra's files", which is the wrong way
    round for a .app: HERE is Contents/Frameworks *inside* the bundle, so the
    bundle itself never matched and Skurra listed itself as an app to remove.
    """
    if app.stem.lower() == "skurra":
        return True
    try:
        return HERE == app or HERE.is_relative_to(app)
    except (AttributeError, ValueError):
        return str(HERE).lower().startswith(str(app).lower() + os.sep)


PERMS_FILE = DATA / "permissions.json"

def load_perms():
    try:
        got = json.loads(PERMS_FILE.read_text())
        return {"asked": bool(got.get("asked")),
                "version": str(got.get("version") or ""),
                "folders": dict(got.get("folders") or {}),
                "grant_versions": dict(got.get("grant_versions") or {})}
    except Exception:
        return {"asked": False, "version": "", "folders": {}, "grant_versions": {}}


def save_perms(state):
    try:
        PERMS_FILE.write_text(json.dumps(state, indent=1))
    except OSError:
        pass


GUARDED_FILE = DATA / "guarded.json"

def load_guarded():
    try:
        return set(json.loads(GUARDED_FILE.read_text()))
    except Exception:
        return set()


def save_guarded(paths):
    try:
        GUARDED_FILE.write_text(json.dumps(sorted(paths)))
    except OSError:
        pass


def without_hanging(fn, seconds):
    """Run fn in its own thread and walk away if it does not come back.

    macOS guards some folders. Reading one puts a permission dialog on the
    screen and the reading thread stops dead until somebody answers it, which
    used to freeze the whole app. We cannot unblock that thread, but we can
    stop waiting for it and carry on.
    """
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = None

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("value") if "value" in box else None


STUCK_FILE = DATA / "stuck.json"

def still_locked(path):
    """Is another program still holding this? Asks without deleting anything.

    On Windows an exclusively locked file cannot even be opened for append,
    which is the whole reason these turn up. If it opens, the lock is gone.
    """
    def one(f):
        try:
            with open(f, "ab"):
                return False
        except OSError:
            return True

    if not os.path.exists(path):
        return False                      # gone: nothing to hide any more
    if not os.path.isdir(path):
        return one(path)

    # A folder is still held if anything inside it is. Look at a sample,
    # newest first, so this stays quick on a cache with thousands of files.
    stop, seen = time.time() + 2, 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            if one(os.path.join(root, f)):
                return True
            seen += 1
            if seen > 300 or time.time() > stop:
                return False
    return False


def load_stuck():
    try:
        return set(json.loads(STUCK_FILE.read_text()))
    except Exception:
        return set()


def save_stuck(paths):
    try:
        STUCK_FILE.write_text(json.dumps(sorted(paths)))
    except OSError:
        pass


HISTORY = DATA / "history.csv"
MOVED_LOG = DATA / "moved.log"

# Never even look inside these. Some pull files down from iCloud and hang
# for minutes; others are private (contacts, call history, iPhone backups)
# and make macOS throw up a permission dialog that freezes the scan.
SKIP = {"clouddocs", "com.apple.clouddocs", "mobile documents",
        "fileprovider", "com.apple.fileprovider",
        "addressbook", "callhistorydb", "callhistorytransactions",
        "knowledge", "mobilesync", "icloud", "mail", "messages", "notes",
        "calendars", "reminders", "safari",
        # Windows: the system's own, Store apps, OneDrive, and installed programs
        "microsoft", "packages", "onedrive", "programs", "temp", "connecteddevicesplatform"}

# Inside ~/Library/Application Support these hold real work.
KEEP_APPDATA = {
    "google", "code", "cursor", "firefox", "chromium", "brave-browser",
    "addressbook", "mobilesync", "clouddocs", "icloud", "mail", "notes",
    "knowledge", "fileprovider", "callhistorydb", "callhistorytransactions",
    "1password", "1password 7", "1password 8", "signal", "whatsapp",
    "telegram desktop", "bitwarden", "keybase",
    "adobe", "figma", "sketch", "affinity", "blender",
    "jetbrains", "sublime text", "docker", "postgres",
    "minecraft", "steam", "skurra",
    "mozilla", "discord", "spotify", "zoom", "obs-studio", "epic", "battle.net",
}

# Inside ~/Library/Caches these are not really caches, whatever the name says.
KEEP_CACHES = {
    "com.spotify.client",
    "com.apple.photos", "com.apple.photoanalysisd",
}

# A cache folder holding a single file this big is probably game or media
# content that took real time to download. Steam is the usual suspect.
BIG_FILE = 100 * 1024 * 1024

# ---------------------------------------------------------------- state

progress = {"done": 0, "total": 0, "where": ""}

# Only one scan at a time. A second request (another tab, a refresh) waits
# for the running one and gets its result instead of clobbering progress.
scan_lock = threading.Lock()
last_scan = {"items": [], "skipped": []}

# A full scan can take minutes, and the window Skurra draws itself in hangs up
# on any single request after about a minute. So the scan never happens inside
# a request: it runs in its own thread and the page watches this.
scan_state = {"running": False, "ready": False, "error": ""}

previous = {}
if HISTORY.exists():
    for line in HISTORY.read_text().splitlines()[1:]:
        bits = line.split(",")
        if len(bits) < 4:
            continue
        try:
            previous[",".join(bits[1:-2])] = (int(bits[-2]), bits[0])
        except ValueError:
            pass


def ago(stamp):
    try:
        then = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    mins = (datetime.datetime.now() - then).total_seconds() / 60
    if mins < 2:
        return "just now"
    if mins < 90:
        return f"{round(mins)} min ago"
    if mins < 2160:
        return f"{round(mins / 60)} hr ago"
    return f"{round(mins / 1440)} days ago"


def precious_cache(path):
    """Looks inside a Caches folder. Returns (protect?, reason)."""
    p = Path(path)
    low = p.name.lower()
    if low in KEEP_CACHES:
        return True, "holds real data despite sitting in Caches"
    if "steam" in low:
        if (p / "steamapps").exists():
            return True, "holds downloaded games"
        for root, dirs, files in os.walk(p, onerror=lambda e: None):
            for f in files:
                try:
                    if os.path.getsize(os.path.join(root, f)) > BIG_FILE:
                        return True, "holds large game files"
                except OSError:
                    pass
    return False, ""


def is_apple(app):
    if WINDOWS:
        return False
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier", "").startswith("com.apple.")
    except Exception:
        return False


def windows_programs():
    """Installed programs from the registry: (name, publisher, folder, size_kb, uninstall)."""
    import winreg
    out, seen = [], set()
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for root, path in roots:
        try:
            key = winreg.OpenKey(root, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                def get(name, default=""):
                    try:
                        return winreg.QueryValueEx(sub, name)[0]
                    except OSError:
                        return default
                name = str(get("DisplayName")).strip()
                if not name or name in seen or get("SystemComponent", 0) == 1:
                    continue
                if not get("UninstallString"):
                    continue
                seen.add(name)
                out.append((name, str(get("Publisher")), str(get("InstallLocation")).strip('"'),
                            int(get("EstimatedSize", 0) or 0), str(get("UninstallString"))))
            except OSError:
                pass
    return out


def app_bundles(folders=None, depth=4):
    """Every real app inside these folders, as Paths.

    Apps do not always sit at the top. A Chrome web app lives in
    "~/Applications/Chrome Apps.localized/Grok.app", one folder deeper than a
    normal download, which is why looking only at the top level missed it.

    This stops the moment it meets a .app, so the helper bundles buried in
    Chrome's and Slack's "Contents/Frameworks" are never mistaken for apps you
    could remove.
    """
    found = []
    for folder in (APP_FOLDERS if folders is None else folders):
        if not folder.exists():
            continue
        base = len(folder.parts)
        for root, dirs, files in os.walk(folder):
            here = Path(root)
            if len(here.parts) - base >= depth:
                dirs.clear()                 # deep enough: no app hides below
                continue
            for d in list(dirs):
                if d == "Contents":          # the inside of a bundle, not an app
                    dirs.remove(d)
                elif d.endswith(".app"):
                    found.append(here / d)
                    dirs.remove(d)           # don't walk inside the app itself
    return found


uninstallers = {}       # Windows: row path -> command that removes the program


def installed_apps():
    """Every app on this machine, as (ids, names). Used to spot leftovers."""
    ids, names = set(), set()
    if WINDOWS:
        for name, publisher, folder, kb, cmd in windows_programs():
            names.add(name.lower())
            if folder:
                names.add(Path(folder).name.lower())
        for folder in [LOCAL / "Programs", Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                       Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]:
            try:
                names.update(d.name.lower() for d in folder.iterdir() if d.is_dir())
            except OSError:
                pass
        return ids, names
    # Apple's own apps count as owners too, so they are included here even
    # though they are never offered for removal.
    for app in app_bundles(APP_FOLDERS + [Path("/System/Applications")]):
        names.add(app.stem.lower())
        try:
            with open(app / "Contents/Info.plist", "rb") as f:
                bid = plistlib.load(f).get("CFBundleIdentifier")
                if bid:
                    ids.add(bid.lower())
        except Exception:
            pass
    return ids, names


def has_an_owner(name, ids, names):
    stem = name.lower()
    if stem in names or stem in ids:
        return True
    for bid in ids:                 # com.webcatalog.jordan.ShipIt
        if stem.startswith(bid + "."):
            return True
    for n in names:                 # "Google" owns "Google Chrome"
        if n.startswith(stem + " ") or stem.startswith(n + " "):
            return True
    return False


def _as_stamp(raw):
    raw = raw.strip()
    if not raw or raw == "(null)":
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %I:%M:%S %p %z"):
        try:
            return datetime.datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            pass
    return 0


def last_used(path):
    """When Spotlight last saw this opened, as a timestamp. 0 if unknown."""
    if WINDOWS:
        return 0
    r = subprocess.run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", str(path)],
                       capture_output=True, text=True)
    return _as_stamp(r.stdout)


def last_used_many(paths):
    """The same, for every app at once: {path: timestamp}.

    Asking mdls once per app cost about 0.8 seconds each, which was most of a
    minute on its own. One call answers for all of them in under a second.

    mdls answers positionally, with no filename to match on, so a file that
    disappears mid-list would shift every later answer up by one and pin one
    app's date on another. If the count does not come back exactly right we
    throw the batch away and ask one at a time.
    """
    paths = list(paths)
    if WINDOWS or not paths:
        return {}
    here = [p for p in paths if os.path.exists(p)]
    out = {}
    try:
        r = subprocess.run(["mdls", "-name", "kMDItemLastUsedDate"] + [str(p) for p in here],
                           capture_output=True, text=True, timeout=30)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if len(lines) == len(here):
            for path, line in zip(here, lines):
                _, _, value = line.partition("=")
                out[path] = _as_stamp(value)
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    for path in here:                      # the batch did not line up: be safe
        out[path] = last_used(path)
    return out


# ---------------------------------------------------------------- scanning

def measure_bundle(path, seconds):
    """Size of an app bundle, the quick way.

    du is C and walks the same tree several times faster than Python can.
    It only reports a size, which is fine here: an app's age comes from
    Spotlight, and the bundle's own date is the fallback. Returns None if du
    is unavailable or too slow, and the caller walks it properly instead.
    """
    if WINDOWS:
        return None
    try:
        r = subprocess.run(["du", "-sk", str(path)],
                           capture_output=True, text=True, timeout=seconds)
        first = r.stdout.split("\t")[0].strip() if r.stdout else ""
        if first.isdigit():
            return int(first) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def measure(path, tick=None, deadline=None):
    """Returns (total bytes, when anything inside was last changed)."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            return 0, 0

    # One stat per file. os.walk plus getsize plus getmtime asked the disk
    # three times for what scandir already knows, and getsize on a symlink
    # reports the size of whatever it points at, which counted some files
    # inside an app twice over.
    total, newest, since = 0, 0, 0
    stack = [str(path)]
    while stack:
        if deadline and time.time() > deadline:
            break
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in SKIP:    # never enter private folders
                        stack.append(e.path)
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            total += st.st_size
            since += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
        if tick and since > 4_000_000:
            tick(since)
            since = 0
    if tick and since:
        tick(since)
    return total, newest


def scan():
    now = time.time()
    # The ring should leave 0% while the list of folders is still being built.
    progress["done"] = 1
    progress["total"] = 24
    progress["where"] = "Starting"
    ids, names = installed_apps()
    stuck_paths = load_stuck()
    guarded = load_guarded()
    skipped = []
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    found, lines, fresh = [], [], {}
    targets = []

    # one level inside the two big folders
    for folder in FOLDERS:
        if not folder.exists():
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for item in entries:
            low = item.name.lower()
            if low.startswith("com.apple.") or low in SKIP or is_ours(item):
                continue
            targets.append((item, folder.name, "cache" if folder == CACHES_DIR else "idle"))

    for spot in BROWSER_CACHES:
        if spot.is_dir():
            targets.append((spot, spot.parts[-4], "cache"))    # "Chrome", "Edge", ...

    # installed apps
    if WINDOWS:
        uninstallers.clear()
        for name, publisher, folder, kb, cmd in windows_programs():
            if publisher.lower().startswith("microsoft"):
                continue
            where = Path(folder) if folder and Path(folder).is_dir() else None
            key = where if where else Path("uninstall:" + name)
            uninstallers[str(key)] = (name, cmd, kb)
            targets.append((key, "Programs", "app"))
    for app in sorted(app_bundles()):
        if is_apple(app) or is_our_app(app):
            continue
        # Name the folder it actually came from, so a Chrome web app reads
        # "Chrome Apps" rather than pretending to be a normal download.
        where = "Applications" if app.parent in APP_FOLDERS else app.parent.name
        targets.append((app, where.replace(".localized", ""), "app"))

    # logs apps leave behind
    for folder in LOG_DIRS:
        if not folder.exists():
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for item in entries:
            low = item.name.lower()
            if low.startswith(".") or low in SKIP or is_ours(item):
                continue
            targets.append((item, "Logs", "log"))

    # installers sitting in Downloads long after they were used.
    # On a Mac, exists() itself raises the system question and the scan
    # waits there at 0%. Ask permission first, and do not touch the folder
    # until the answer is already yes.
    downloads = next((p for p in protected_folders() if p.name.lower() == "downloads"), HOME / "Downloads")
    if allowed_folder(downloads):
        # If macOS still wants an answer, this returns and the scan carries
        # on. Waiting here is what froze the ring on "Starting".
        entries = without_hanging(lambda: list(downloads.iterdir()), 2)
        if entries is None:
            state = load_perms()
            state["folders"][str(downloads)] = "declined"
            versions = dict(state.get("grant_versions") or {})
            versions.pop(str(downloads), None)
            state["grant_versions"] = versions
            save_perms(state)
            entries = []
        for item in entries:
            try:
                if item.is_file() and item.suffix.lower() in INSTALLER_EXT and not is_ours(item):
                    targets.append((item, "Downloads", "installer"))
            except OSError:
                continue

    # caches tucked inside each app's own data folder
    for appdata in APPDATA_DIRS:
        if not appdata.exists():
            continue
        try:
            apps = list(appdata.iterdir())
        except OSError:
            apps = []
        for app in apps:
            if not app.is_dir() or app.name.lower() in SKIP:
                continue
            for name in CACHE_INSIDE_APPS:
                spot = app / name
                if spot.is_dir():
                    targets.append((spot, app.name, "cache"))

    # One entry per folder, whatever route it arrived by.
    seen_paths, once = set(), []
    for item, where, kind in targets:
        if str(item) in seen_paths:
            continue
        seen_paths.add(str(item))
        once.append((item, where, kind))
    targets = once

    # budget the progress bar by bytes, using last run's sizes
    weights = {}
    for item, where, kind in targets:
        seen = previous.get(f"{where}/{item.name}")
        weights[f"{where}/{item.name}"] = seen[0] if seen else 0

    real = [w for w in weights.values() if w]
    spare = sum(real) / len(real) if real else 1
    for key in weights:
        if not weights[key]:
            weights[key] = spare

    # Hand the same early sliver over to the real total, so the ring stays
    # off 0% instead of jumping backwards when measuring starts.
    progress["total"] = sum(weights.values()) or 1
    progress["done"] = progress["total"] * 0.04

    # Every app's last-opened date in one go, rather than one call each.
    app_dates = last_used_many([i for i, w, k in targets
                                if k == "app" and not str(i).startswith("uninstall:")])

    for item, where, kind in targets:
        # Say what is being measured, so a four-minute scan looks like work
        # happening rather than a hang.
        progress["where"] = f"{where}/{item.name}" if where else item.name
        if str(item) in guarded:
            if not can_read(item, 1.5):        # still shut: leave it be
                skipped.append({"name": item.name, "where": where, "mb": 0,
                                "why": "macOS wants permission for this one"})
                continue
            guarded.discard(str(item))         # they have since let us in
            save_guarded(guarded)
        if str(item) in stuck_paths:
            if still_locked(item):
                try:
                    mb = round(measure(item)[0] / 1024 / 1024, 1)
                except OSError:
                    mb = 0
                skipped.append({"name": item.name, "where": where, "mb": mb,
                                "why": "another program is using it right now"})
                continue
            stuck_paths.discard(str(item))   # the program let go: treat it normally
            save_stuck(stuck_paths)

        key = f"{where}/{item.name}"
        budget = weights[key]
        spent = [0]

        def tick(n, cap=budget, used=spent):
            step = min(n, max(0, cap - used[0]))
            used[0] += step
            progress["done"] += step

        if str(item).startswith("uninstall:"):
            size, touched = uninstallers[str(item)][2] * 1024, 0
            tick(budget)
        elif kind == "app":
            # An app's date comes from Spotlight below, so there is nothing to
            # learn from walking every file inside the bundle. Ask du.
            quick = without_hanging(lambda: measure_bundle(item, ITEM_LIMIT), ITEM_LIMIT + 5)
            if quick is None:
                got = without_hanging(
                    lambda: measure(item, tick, deadline=time.time() + ITEM_LIMIT),
                    ITEM_LIMIT + 5)
            else:
                try:
                    stamp_at = os.path.getmtime(item)
                except OSError:
                    stamp_at = 0
                got = (quick, stamp_at)
                tick(budget)
        else:
            got = without_hanging(
            lambda: measure(item, tick, deadline=time.time() + ITEM_LIMIT),
            ITEM_LIMIT + 5)
        progress["done"] += max(0, budget - spent[0])
        if got is None:                      # macOS is holding this one
            guarded.add(str(item))
            save_guarded(guarded)
            skipped.append({"name": item.name, "where": where, "mb": 0,
                            "why": "macOS wants permission for this one"})
            continue
        size, touched = got

        if size == 0:
            continue

        if kind == "app":
            touched = app_dates.get(item, 0) or touched
        days = int((now - touched) / 86400) if touched else 9999
        lines.append(f"{stamp},{key},{size},{days}")
        fresh[key] = size

        # Too small to be worth a drawer of its own, but still kept and still
        # searchable. Hiding these outright is why a 2 MB app could not be
        # found by name.
        mbs = size / 1024 / 1024
        small = ((kind in ("idle", "cache") and mbs < MIN_MB)
                 or (kind == "log" and mbs < 1)
                 or (kind == "installer" and mbs < 5))

        # how safe is it to remove this?
        level, why = "safe", ""
        if kind == "idle":
            owned = has_an_owner(item.name, ids, names)
            if not owned:
                # The app is gone, so whatever this holds is nobody's work now,
                # however precious the name looked. Uninstalling Cursor should
                # not leave a gigabyte marked "cannot be rebuilt" for ever.
                level, why = "safe", "no installed app owns this any more"
            elif item.name.lower() in KEEP_APPDATA:
                level, why = "important", "holds work that cannot be rebuilt"
            elif days < MIN_DAYS:
                ago_txt = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
                level, why = "important", f"an app used this {ago_txt}"
            else:
                level, why = "less", f"app still installed, data untouched for {days} days"
        elif kind == "log":
            level, why = "safe", "a log an app wrote and never reads again"
        elif kind == "installer":
            if days < INSTALLER_DAYS:
                level = "less"
                why = f"downloaded {days} day{'s' if days != 1 else ''} ago, you may still need it"
            else:
                level, why = "safe", "an installer you already used"
        elif kind == "cache":
            keep, reason = precious_cache(item) if where == "Caches" else (False, "")
            if keep:
                level, why = "important", reason
            else:
                why = "the app rebuilds this when it needs it"

        seen = previous.get(key)
        when = ""
        if seen is None:
            change = "first scan"
        else:
            before, last_time = seen
            when = ago(last_time)
            gap = round((size - before) / 1024 / 1024, 1)
            if abs(size - before) < 102400:
                change = "unchanged"
            else:
                change = f"{'+' if gap > 0 else ''}{gap} MB"

        found.append({
            "name": uninstallers[str(item)][0] if str(item) in uninstallers
                    else item.stem if kind == "app" else item.name,
            "where": where,
            "kind": kind,
            "path": str(item),
            "mb": round(size / 1024 / 1024, 1),
            "days": days,
            "change": change,
            "when": when,
            "level": level,
            "why": why,
            "small": small,
        })

    try:
        head = "" if HISTORY.exists() else "date,key,bytes,days_idle\n"
        with open(HISTORY, "a") as f:
            f.write(head + "\n".join(lines) + "\n")
    except OSError:
        pass

    for key, size in fresh.items():
        previous[key] = (size, stamp)

    progress["total"] = 0
    progress["done"] = 0
    progress["where"] = ""
    # Least-used apps first. Size still orders caches and leftovers.
    found.sort(key=lambda r: (
        0 if r["kind"] == "app" else 1,
        -(r["days"] if r["kind"] == "app" else 0),
        -r["mb"],
    ))
    return {"items": found, "skipped": skipped}


def scan_worker():
    """Runs one scan to the end, in its own thread, and never raises."""
    global last_scan
    try:
        last_scan = scan()
        scan_state["error"] = ""
    except Exception as e:
        scan_state["error"] = str(e) or e.__class__.__name__
    finally:
        # Order matters: the page only trusts "ready" once "running" is false,
        # so clear running last.
        scan_state["ready"] = True
        scan_state["running"] = False
        progress["where"] = ""
        scan_lock.release()


def start_scan():
    """Begins a scan if one is not already going. Returns at once, either way.

    A scan takes minutes, and the window gives up on a request after about
    one. So no request ever waits for it: this starts the work and the page
    watches /api/progress until it finishes.
    """
    if not scan_lock.acquire(blocking=False):
        return {"started": False, "running": True}
    scan_state.update(running=True, ready=False, error="")
    progress.update(done=0, total=1, where="")
    threading.Thread(target=scan_worker, daemon=True).start()
    return {"started": True, "running": True}


def scan_status():
    return {"done": progress["done"], "total": progress["total"],
            "where": progress["where"], "running": scan_state["running"],
            "ready": scan_state["ready"], "error": scan_state["error"]}


def scan_result():
    out = dict(last_scan)
    out["error"] = scan_state["error"]
    return out


# ---------------------------------------------------------------- disk walk

explore_progress = {"done": 0, "total": 1, "where": ""}
explore_lock = threading.Lock()


def norm_key(path):
    return os.path.normcase(os.path.normpath(str(path)))


def in_home_text(path):
    """True when the path is the home folder or inside it. No resolve(), no dialog."""
    raw = norm_key(path)
    home = norm_key(HOME)
    return raw == home or raw.startswith(home + os.sep)


def guarded_folder(path):
    """Desktop, Downloads, and the other folders that stay closed until allowed."""
    raw = norm_key(path)
    for root in protected_folders():
        base = norm_key(root)
        if base and (raw == base or raw.startswith(base + os.sep)):
            return root
    return None


_protected_cache = None


def protected_folders():
    """The folders Skurra leaves closed until the person allows them."""
    global _protected_cache
    if _protected_cache is not None:
        return _protected_cache
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music"]
    names.append("Videos" if WINDOWS else "Movies")
    if WINDOWS:
        _protected_cache = [windows_known(n) for n in names]
    else:
        _protected_cache = [HOME / n for n in names]
    return _protected_cache


def can_explore(path):
    """May Disk space open this folder?

    String compare only. resolve() on Downloads or Desktop is enough to raise
    the Mac question, and treating that as "outside home" is why Open location
    showed an error instead of the file. Apps also live outside the home
    folder, on both Mac and Windows, and the Clean up list already named them.
    A search can also land on another disk, so those folders open too.
    """
    raw = norm_key(path)
    home = norm_key(HOME)
    if raw == home or raw.startswith(home + os.sep):
        return True
    roots = [Path(p) for p in APP_FOLDERS]
    if WINDOWS:
        for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            folder = os.environ.get(key)
            if folder:
                roots.append(Path(folder))
    for root in roots:
        base = norm_key(root)
        if raw == base or raw.startswith(base + os.sep):
            return True
    if WINDOWS:
        if len(raw) >= 3 and raw[1] == ":":
            rest = raw[2:].lstrip("\\/")
            top = rest.split("\\")[0].split("/")[0].casefold()
            if top in {"windows", "program files", "program files (x86)", "programdata",
                       "$recycle.bin", "system volume information", "recovery"}:
                return False
            return True
        return False
    low = raw.lower()
    if low == "/applications" or low.startswith("/applications" + os.sep):
        return True
    if low.startswith("/volumes" + os.sep):
        return True
    return False


# Names that hang a walk or belong to the system, not to a folder someone wants to bin.
SEARCH_SKIP = {
    "clouddocs", "com.apple.clouddocs", "mobile documents",
    "fileprovider", "com.apple.fileprovider",
    "node_modules", ".git", ".svn", "__pycache__",
    "$recycle.bin", "system volume information", "recovery",
    "windows", "program files", "program files (x86)", "programdata",
}


def search_starts():
    """Where a whole-computer search begins. Home first, then other disks."""
    found, seen = [], set()

    def add(path):
        key = norm_key(path)
        if not key or key in seen:
            return
        seen.add(key)
        found.append(os.path.normpath(str(path)))

    add(HOME)
    if WINDOWS:
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    add(f"{chr(65 + i)}:\\")
        except Exception:
            pass
        for root in disk_roots():
            add(root)
        return found
    add("/Applications")
    try:
        boot = os.stat("/").st_dev
    except OSError:
        boot = None
    try:
        for name in os.listdir("/Volumes"):
            if name.startswith("."):
                continue
            path = os.path.join("/Volumes", name)
            try:
                if boot is not None and os.stat(path).st_dev == boot:
                    continue
            except OSError:
                continue
            add(path)
    except OSError:
        pass
    return found


def search_hit(path, name, is_dir):
    parent = str(Path(path).parent)
    return {
        "name": name,
        "path": path,
        "dir": bool(is_dir),
        "kind": kind_of(path, bool(is_dir)),
        "mb": 0,
        "files": 0,
        "days": 0,
        "approx": True,
        "where": "Home" if norm_key(parent) == norm_key(HOME) else (Path(parent).name or parent),
    }


GUARDED_NAMES = {"desktop", "documents", "downloads", "pictures", "movies", "music", "videos"}


def search_list(path, seconds=2):
    """Names in one folder. Gives up if macOS holds the call, and never follows links.

    Asking is_dir() on Downloads while listing Home is enough to raise the Mac
    question and throw the whole listing away. Those names are folders. Do not
    stat them from their parent.
    """
    def snap(folder=path):
        out = []
        parent_is_home = norm_key(folder) == norm_key(HOME)
        for entry in os.scandir(folder):
            name = entry.name
            if name.startswith("."):
                continue
            if parent_is_home and name.casefold() in GUARDED_NAMES:
                out.append((name, entry.path, True))
                continue
            is_dir = False
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = "." not in name
            out.append((name, entry.path, is_dir))
        return out
    return without_hanging(snap, seconds)


def note_blocked(blocked, seen, folder):
    key = norm_key(folder)
    if key in seen:
        return
    seen.add(key)
    blocked.append({"name": Path(folder).name or str(folder), "path": str(folder)})


def search_exact(query):
    """A typed path, such as /Users/you/Downloads/bebas_kai, opened by its parent listing."""
    text = str(query or "").strip().strip('"').strip("'")
    if text.startswith("~"):
        text = os.path.expanduser(text)
    slashed = "/" in text or "\\" in text or (len(text) > 2 and text[1] == ":")
    if not slashed:
        return None
    if not (text.startswith("/") or text.startswith("\\\\") or (len(text) > 2 and text[1] == ":")):
        text = os.path.join(str(HOME), text)
    text = os.path.normpath(text)
    parent = os.path.dirname(text)
    leaf = os.path.basename(text)
    if not leaf or not parent:
        return [], []
    blocked, seen = [], set()
    guard = guarded_folder(parent) or guarded_folder(text)
    if guard is not None and not allowed_folder(guard):
        note_blocked(blocked, seen, guard)
        return [], blocked
    if not (can_explore(parent) or can_explore(text)):
        return [], blocked
    entries = search_list(parent, 8 if guard is not None else 3)
    if entries is None:
        if guard is not None:
            note_blocked(blocked, seen, guard)
        return [], blocked
    want = leaf.casefold()
    hits = []
    for name, path, is_dir in entries:
        if name.casefold() == want or want in name.casefold():
            hits.append(search_hit(path, name, is_dir))
    hits.sort(key=lambda item: (item["name"].casefold() != want, item["name"].casefold()))
    return hits, blocked


def search_walk(query, limit=50):
    """Every disk, by name. Shallow folders come first, so Downloads is not behind a deep tree."""
    q = str(query or "").strip().casefold()
    hits, blocked, seen = [], [], set()
    if len(q) < 2:
        return hits, blocked
    # Say which of the protected folders were not opened, even if listing Home
    # itself gets stuck. Otherwise a folder in Downloads looks like it is missing.
    for folder in protected_folders():
        if not allowed_folder(folder):
            note_blocked(blocked, seen, folder)
    deadline = time.time() + 12
    queue = deque()
    for start in search_starts():
        guard = guarded_folder(start)
        if guard is not None and not allowed_folder(guard):
            note_blocked(blocked, seen, guard)
            continue
        queue.append(start)
    while queue and time.time() < deadline and len(hits) < limit:
        current = queue.popleft()
        guard = guarded_folder(current)
        if guard is not None and not allowed_folder(guard):
            note_blocked(blocked, seen, guard)
            continue
        seconds = 8 if guard is not None else 3
        entries = search_list(current, seconds)
        if entries is None:
            if guard is not None:
                note_blocked(blocked, seen, guard)
            continue
        for name, path, is_dir in entries:
            low = name.casefold()
            if q in low and len(hits) < limit:
                hits.append(search_hit(path, name, is_dir))
            if not is_dir or len(hits) >= limit:
                continue
            if low in SEARCH_SKIP or is_library(name) or low.endswith(".app"):
                continue
            # ~/Library and AppData are the clean-up scan, and reading them raises dialogs.
            parent = norm_key(Path(path).parent)
            if parent == norm_key(HOME) and low in {"library", "appdata"}:
                continue
            child = guarded_folder(path)
            if child is not None and not allowed_folder(child):
                note_blocked(blocked, seen, child)
                continue
            queue.append(path)
    hits.sort(key=lambda item: (
        0 if item["name"].casefold() == q else 1 if item["name"].casefold().startswith(q) else 2,
        not item["dir"],
        item["name"].casefold(),
    ))
    return hits[:limit], blocked


def search_system(query):
    """Find a file or folder anywhere the computer will let Skurra look."""
    text = str(query or "").strip()
    if len(text) < 2:
        return {"query": text, "items": [], "blocked": [], "partial": False}
    exact, blocked = [], []
    looked = search_exact(text)
    if looked is not None:
        exact, blocked = looked
        if any(item["name"].casefold() == os.path.basename(text).strip('"').strip("'").casefold()
               for item in exact):
            return {"query": text, "items": exact[:50], "blocked": blocked, "partial": False}
        if blocked:
            return {"query": text, "items": exact[:50], "blocked": blocked, "partial": False}
    leaf = text
    if "/" in text or "\\" in text:
        leaf = os.path.basename(os.path.normpath(text.strip('"').strip("'")))
    more, more_blocked = search_walk(leaf or text)
    seen = {norm_key(item["path"]) for item in exact}
    items = list(exact)
    for item in more:
        if norm_key(item["path"]) in seen:
            continue
        seen.add(norm_key(item["path"]))
        items.append(item)
    have = {norm_key(item["path"]) for item in blocked}
    for item in more_blocked:
        if norm_key(item["path"]) not in have:
            blocked.append(item)
            have.add(norm_key(item["path"]))
    return {"query": text, "items": items[:50], "blocked": blocked, "partial": len(items) >= 50}


def focus_stub(focus, parent):
    """The one file Open location asked for, even if the folder listing hid it."""
    focus = str(focus or "")
    if not focus:
        return None
    child = Path(focus)
    parent_s = os.path.normcase(os.path.normpath(str(parent)))
    child_parent = os.path.normcase(os.path.normpath(str(child.parent)))
    if parent_s != child_parent or child.name in ("", ".", ".."):
        return None
    suffix = child.suffix.lower()
    is_dir = suffix == ".app" or suffix not in INSTALLER_EXT and "." not in suffix
    if suffix in INSTALLER_EXT:
        is_dir = False
    return {
        "name": child.name,
        "path": focus,
        "dir": is_dir,
        "kind": kind_of(focus, is_dir),
        "mb": 0,
        "files": 1,
        "days": 0,
        "approx": False,
    }


def attach_focus(items, parent, focus):
    stub = focus_stub(focus, parent)
    if not stub:
        return items
    key = os.path.normcase(os.path.normpath(stub["path"]))
    for it in items:
        if os.path.normcase(os.path.normpath(it["path"])) == key:
            return items
    items.insert(0, stub)
    return items


def folder_size(path, deadline=None):
    """Bytes inside a folder, how many files, and whether it got to the end."""
    total, count, whole = 0, 0, True
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP
                   and not is_library(d) and not is_ours(os.path.join(root, d))]
        if deadline and time.time() > deadline:
            whole = False
            break
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count, whole


def disk_meter(path):
    """How full the disk holding this folder is. Bytes only, no file names."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {}
    return {
        "totalMb": round(usage.total / 1024 / 1024, 1),
        "usedMb": round(usage.used / 1024 / 1024, 1),
        "freeMb": round(usage.free / 1024 / 1024, 1),
    }


THUMB_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
THUMB_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def thumb_bytes(path):
    """A small picture for a file the disk view is already allowed to list.

    Large photos are reduced with sips on a Mac. Anything Skurra cannot
    shrink, and anything over a few megabytes, is left as an icon.
    """
    ext = Path(path).suffix.lower()
    if ext not in THUMB_EXT or not can_explore(path):
        return None
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        mtime = int(os.path.getmtime(path))
    except OSError:
        return None
    if size <= 0 or size > 20 * 1024 * 1024:
        return None
    if not WINDOWS and size > 350_000:
        folder = DATA / "thumbs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha1(f"{path}|{mtime}|{size}".encode()).hexdigest()[:24]
            dest = folder / f"{key}.jpg"
            if dest.exists() and dest.stat().st_size > 0:
                return dest.read_bytes(), "image/jpeg"
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", "320", path, "--out", str(dest)],
                capture_output=True, timeout=8)
            if dest.exists() and dest.stat().st_size > 0:
                return dest.read_bytes(), "image/jpeg"
        except (OSError, subprocess.SubprocessError):
            pass
    if size > 3 * 1024 * 1024:
        return None
    try:
        return Path(path).read_bytes(), THUMB_MIME[ext]
    except OSError:
        return None


def list_folder(p):
    return [e for e in os.scandir(p)
            if not e.name.startswith(".")
            and e.name.lower() not in SKIP
            and not is_library(e.name)
            and not (p == HOME and e.name.lower() in NOT_YOURS)
            and not is_ours(e.path)]


def explore(path, focus=""):
    """Everything one level inside a folder, each with its true size."""
    p = Path(path)
    guard = guarded_folder(str(p))
    if guard is not None and not allowed_folder(guard):
        return {"error": "allow", "allow": str(guard), "name": guard.name,
                "path": str(p), "parent": str(p.parent), "crumbs": [
                    {"name": guard.name, "path": str(guard)}],
                "items": [], "totals": {}, "mb": 0}
    # Even listing a folder can stop dead behind a macOS permission dialog,
    # so this gets the same deadline as measuring does.
    entries = without_hanging(lambda: list_folder(p), 8)
    if entries is None:
        guarded = load_guarded()
        guarded.add(str(p))
        save_guarded(guarded)
        pinned = attach_focus([], p, focus)
        if pinned:
            return {"path": str(p), "parent": str(p.parent), "crumbs": [
                        {"name": p.name or str(p), "path": str(p)}],
                    "items": pinned, "totals": {},
                    "mb": 0,
                    "note": "The rest of this folder could not be read. This file is still here."}
        return {"error": "macOS wants permission for this folder", "path": str(p),
                "parent": str(p.parent), "crumbs": [], "items": [], "totals": {}, "mb": 0}

    explore_progress.update(done=0, total=max(1, len(entries)), where=p.name or str(p))
    items = []
    ends = time.time() + DISK_BUDGET
    for n, e in enumerate(entries):
        explore_progress["done"] = n
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        size, files, whole, touched = 0, 0, False, 0
        try:
            if is_dir:
                left = max(0.5, min(DISK_ITEM, ends - time.time()))
                got = without_hanging(
                    lambda: folder_size(e.path, deadline=time.time() + left), left + 3)
                # A slow or guarded folder used to vanish from the list, so a
                # search for its name said it was not there.
                if got is not None:
                    size, files, whole = got
            else:
                size, files, whole = e.stat(follow_symlinks=False).st_size, 1, True
            try:
                touched = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                touched = 0
        except OSError:
            if not is_dir:
                continue
        items.append({
            "name": e.name,
            "path": e.path,
            "dir": is_dir,
            "kind": kind_of(e.path, is_dir),
            "mb": round(size / 1024 / 1024, 1),
            "files": files,
            "days": int((time.time() - touched) / 86400) if touched else 0,
            "approx": not whole,
        })
    explore_progress["done"] = explore_progress["total"]

    # Forgotten first. A folder touched today stays lower than one idle for a year.
    items.sort(key=lambda r: (-(r["days"] or 0), -r["mb"], r["name"].casefold()))
    items = attach_focus(items, p, focus)
    totals = {}
    for it in items:
        k = "folder" if it["dir"] else it["kind"]
        totals[k] = round(totals.get(k, 0) + it["mb"], 1)

    parent = str(p.parent) if p != Path(p.anchor) and p != HOME else ""
    crumbs = []
    walk = p
    while True:
        crumbs.append({"name": "Home" if walk == HOME else (walk.name or str(walk)), "path": str(walk)})
        if walk == HOME or walk == Path(walk.anchor) or len(crumbs) > 12:
            break
        walk = walk.parent
    crumbs.reverse()

    return {"path": str(p), "parent": parent, "crumbs": crumbs,
            "items": items, "totals": totals,
            "disk": disk_meter(str(p)),
            "mb": round(sum(i["mb"] for i in items), 1)}


def explore_once(path, focus=""):
    """One walk at a time, but never queue forever behind a stuck one."""
    if not explore_lock.acquire(timeout=45):
        pinned = attach_focus([], path, focus)
        if pinned:
            return {"path": str(path), "parent": str(Path(path).parent), "crumbs": [
                        {"name": Path(path).name or str(path), "path": str(path)}],
                    "items": pinned, "totals": {}, "mb": 0,
                    "note": "Skurra is still measuring another folder. This file is shown on its own."}
        return {"error": "Skurra is still measuring another folder", "path": str(path),
                "parent": "", "crumbs": [], "items": [], "totals": {}, "mb": 0}
    try:
        return explore(path, focus)
    finally:
        explore_lock.release()


def inside_home(path):
    """True when a path really sits inside the user's own folder."""
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    try:
        p.relative_to(HOME.resolve())
    except ValueError:
        return False
    return p != HOME.resolve()


# ---------------------------------------------------------------- safety

def deletable(path):
    """A folder the person searched for and may move to the Trash.

    Compared as text. resolve() on Downloads is itself the Mac question, and
    that question from the delete path is why a found folder could not be removed.
    """
    raw = norm_key(path)
    if not raw or raw in (os.sep, "/", "\\"):
        return False
    home = norm_key(HOME)
    if raw == home:
        return False
    if in_home_text(path):
        parent = norm_key(Path(os.path.normpath(str(path))).parent)
        name = Path(os.path.normpath(str(path))).name.casefold()
        protected = {"desktop", "documents", "downloads", "pictures", "movies", "music", "videos"}
        if parent == home and name in protected:
            return False
        for folder in APPDATA_DIRS:
            base = norm_key(folder)
            if raw == base or raw.startswith(base + os.sep):
                return False
        library = home + os.sep + norm_key("Library")
        if raw == library or raw.startswith(library + os.sep):
            return False
        return True
    if WINDOWS:
        if len(raw) < 4 or raw[1] != ":":
            return False
        if raw.rstrip("\\/")[1:] in (":",):
            return False
        rest = raw[2:].lstrip("\\/")
        if not rest:
            return False
        top = rest.split("\\")[0].split("/")[0].casefold()
        if top in {"windows", "program files", "program files (x86)", "programdata",
                   "$recycle.bin", "system volume information", "recovery"}:
            return False
        return True
    low = raw.lower()
    if low.startswith("/volumes/"):
        parts = [p for p in low.split("/") if p]
        return len(parts) >= 3
    return False


def allowed(path):
    """Returns (yes_or_no, reason). A path has to clear every gate."""
    if is_ours(path):
        return False, "Skurra is running from here"
    if known_program(path):
        return True, ""                      # a Windows program, removed by its uninstaller
    if deletable(path):
        return True, ""
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "does not exist"

    caches = CACHES_DIR.resolve()
    appdatas = [d.resolve() for d in APPDATA_DIRS]

    if any(p == f.resolve() for f in FOLDERS):
        return False, "that is an entire scan folder"

    if p.parent == caches:
        return True, ""          # the warning is on the row; the choice is theirs

    if any(p.parent == d.resolve() for d in LOG_DIRS if d.exists()):
        return True, ""

    downloads = HOME / "Downloads"
    if p.parent == downloads and p.suffix.lower() in INSTALLER_EXT:
        return True, ""

    if p.parent in appdatas:
        # KEEP_APPDATA is a warning, not a veto: app data goes to the Trash and
        # can be dragged back. SKIP is different, those are the private folders
        # macOS guards and Skurra has no business in.
        if p.name.lower() in SKIP:
            return False, "one of the private folders Skurra leaves alone"
        return True, ""

    if p.name in CACHE_INSIDE_APPS and p.parent.parent in appdatas:
        return True, ""

    if any(p == b.resolve() for b in BROWSER_CACHES if b.exists()):
        return True, ""

    for folder in APP_FOLDERS:
        if p.parent == folder.resolve() and p.suffix == ".app":
            if is_apple(p):
                return False, "an Apple app"
            return True, ""

    # Anything of the user's own, inside their home folder. Trash only.
    # Compare the names only. resolve() on Desktop or Downloads is enough
    # to raise the Mac question, and this check runs while deleting.
    if inside_home(p):
        protected = {
            "Desktop", "Documents", "Downloads", "Pictures",
            "Movies", "Music", "Videos",
        }
        if p == HOME or (p.name in protected and p.parent == HOME):
            return False, "that is a whole folder like Documents"
        if any(str(p).lower().startswith(str(d.resolve()).lower() + os.sep) for d in APPDATA_DIRS):
            return False, "app data, use the App data list"
        return True, ""

    return False, "not somewhere Skurra scans"


def is_cache(path):
    """Only these may be wiped in place instead of going to the Trash."""
    p = Path(path)
    caches = CACHES_DIR.resolve()
    appdatas = [d.resolve() for d in APPDATA_DIRS]
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (p.parent == caches
            or (p.name in CACHE_INSIDE_APPS and p.parent.parent in appdatas)
            or any(p.parent == d.resolve() for d in LOG_DIRS if d.exists())
            or any(p == b.resolve() for b in BROWSER_CACHES if b.exists()))


def reveal(path):
    """Show this file in Finder or File Explorer. Nothing is opened inside Skurra.

    The path is only accepted when Disk space is already allowed to look at
    its folder. open and explorer do the revealing, so Skurra itself does not
    touch the folder and raise a permission question.
    """
    path = str(path or "")
    if not path or path.startswith("uninstall:") or "\x00" in path:
        return False
    parent = str(Path(path).parent)
    if parent in ("", ".", path):
        return False
    if not (can_explore(path) or can_explore(parent)):
        return False
    if WINDOWS:
        subprocess.Popen(["explorer", "/select," + path])
        return True
    opened = subprocess.run(["open", "-R", path], capture_output=True)
    if opened.returncode != 0:
        opened = subprocess.run(["open", parent], capture_output=True)
    return opened.returncode == 0


def known_program(path):
    """The uninstaller key for this path, ignoring slash and capital differences."""
    want = os.path.normcase(os.path.normpath(str(path)))
    for key in uninstallers:
        if os.path.normcase(os.path.normpath(str(key))) == want:
            return key
    return ""


def to_trash(path):
    key = known_program(path)
    if key:
        # Windows programs are not files to move; hand over to their uninstaller.
        name, cmd, kb = uninstallers[key]
        try:
            subprocess.Popen(cmd, shell=True)
            return True
        except OSError:
            return False
    if WINDOWS:
        from send2trash import send2trash
        try:
            send2trash(str(path))
            return True
        except Exception:
            return False
    script = f'tell application "Finder" to delete POSIX file "{path}"'
    r = subprocess.run(["osascript", "-e", script], capture_output=True)
    return r.returncode == 0


def clear(path):
    """Empty a cache folder. The folder itself stays.

    Windows locks files that a running program still has open, so a browser
    cache can rarely be wiped whole while the browser is running. Removing
    most of it is still worth doing, so this reports what it managed:
    (bytes freed, files left behind).
    """
    freed, stuck = 0, 0

    def size_of(p):
        try:
            if os.path.isfile(p):
                return os.path.getsize(p)
        except OSError:
            return 0
        total = 0
        for root, dirs, files in os.walk(p, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    if is_ours(path):
        return 0, -1

    # A cache can be a single file, not just a folder. Delete it on its own.
    if os.path.isfile(path):
        before = size_of(path)
        try:
            os.unlink(path)
        except OSError:
            return 0, 1                   # Windows holds it open
        return before, 0

    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, -1                      # could not even look inside

    for e in entries:
        if is_ours(e.path):
            continue
        before = size_of(e.path)
        try:
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path, onerror=lambda *a: None)
            else:
                os.unlink(e.path)
        except OSError:
            pass
        after = size_of(e.path) if os.path.exists(e.path) else 0
        freed += max(0, before - after)
        if after:
            stuck += 1
    return freed, stuck


def empty_trash():
    if WINDOWS:
        import ctypes
        # 1 no confirmation, 2 no progress window, 4 no sound
        return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 1 | 2 | 4) in (0, -2147418113)
    script = """
    tell application "Finder"
        set w to warns before emptying of trash
        set warns before emptying of trash to false
        empty trash
        set warns before emptying of trash to w
    end tell"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300)
    return r.returncode == 0


def bin_state():
    """What is in the Trash. macOS hides ~/.Trash from us unless the user
    grants Full Disk Access, so ask Finder, and fall back to our own log."""
    items = None
    if WINDOWS:
        import ctypes
        class Info(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]
        info = Info(ctypes.sizeof(Info), 0, 0)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            items = int(info.i64NumItems)
    try:
        if items is None:
            items = len([e for e in os.listdir(TRASH) if e != ".DS_Store"])
    except (OSError, TypeError):
        try:
            r = subprocess.run(["osascript", "-e",
                                'tell application "Finder" to count of items of trash'],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                items = int(r.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError):
            pass

    # what Skurra itself has put there since the Trash was last emptied
    ours, mb = 0, 0.0
    for e in recent(500):
        if e.get("did") == "emptied":
            break
        if e.get("did") == "trashed":
            ours += 1
            mb += e.get("mb") or 0

    return {"items": items, "ours": ours, "mb": round(mb, 1)}


def bin_items():
    """What is sitting in the Trash, by name and size.

    macOS will not let us read ~/.Trash without Full Disk Access, and Windows
    hides the Recycle Bin the same way, so both are asked through the shell
    that owns them.
    """
    if WINDOWS:
        ps = ("$s=New-Object -ComObject Shell.Application;"
              "$s.Namespace(10).Items() | ForEach-Object {"
              "  $n=$_.Name; $z=$s.Namespace(10).GetDetailsOf($_,3);"
              "  Write-Output \"$n`t$z\" }")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out = []
        for line in r.stdout.splitlines():
            name, _, _size = line.partition("\t")
            if name.strip():
                out.append({"name": name.strip(), "mb": 0})
        return out

    def ask(script):
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=25)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    names = ask('tell application "Finder" to get name of every item of trash')
    if not names:
        return []
    names = [n.strip() for n in names.split(",") if n.strip()]
    sizes = ask('tell application "Finder" to get size of every item of trash')
    numbers = []
    for bit in sizes.split(","):
        try:
            numbers.append(float(bit.strip()))
        except ValueError:
            numbers.append(0)

    out = []
    for n, name in enumerate(names):
        by = numbers[n] if n < len(numbers) else 0
        out.append({"name": name, "mb": round(by / 1024 / 1024, 1)})
    out.sort(key=lambda r: r["mb"], reverse=True)
    return out


def bin_remove(names):
    """Delete these, by name, out of the Trash for good."""
    gone, failed = 0, []
    for name in names:
        if not name or any(c in name for c in '"\\'):
            failed.append(name)
            continue
        if WINDOWS:
            ps = ("$s=New-Object -ComObject Shell.Application;"
                  "$s.Namespace(10).Items() | Where-Object { $_.Name -eq "
                  f"'{name}'" + " } | ForEach-Object {"
                  " Remove-Item -LiteralPath $_.Path -Recurse -Force }")
            cmd = ["powershell", "-NoProfile", "-Command", ps]
        else:
            cmd = ["osascript", "-e",
                   f'tell application "Finder" to delete (every item of trash whose name is "{name}")']
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode == 0:
                gone += 1
            else:
                failed.append(name)
        except (OSError, subprocess.TimeoutExpired):
            failed.append(name)
    return gone, failed


def record(entry):
    try:
        with open(MOVED_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def recent(n=25):
    if not MOVED_LOG.exists():
        return []
    out = []
    for line in MOVED_LOG.read_text().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    out.reverse()
    return out


# ---------------------------------------------------------------- updates

def as_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


update_cache = None
last_swap = None

def swap_report():
    """Read how the last swap went, once, and hand it to the page."""
    global last_swap
    if last_swap is None:
        last_swap = last_update_result() or {}
    return last_swap


def check_update():
    """Ask latest.json what is out. A failed look is not remembered, so a
    machine that was offline at launch still finds the update later."""
    global update_cache
    if update_cache is not None and update_cache.get("latest") != update_cache.get("current"):
        return update_cache
    info = {"current": VERSION, "latest": VERSION, "available": False, "url": "", "notes": "",
            "os": "windows" if WINDOWS else "mac"}
    try:
        with urllib.request.urlopen(UPDATE_URL, timeout=6, context=HTTPS) as r:
            latest = json.loads(r.read().decode())
        info["latest"] = str(latest.get("version", VERSION))
        info["url"] = str(latest.get("url", ""))
        info["notes"] = str(latest.get("notes", ""))
        info["available"] = as_tuple(info["latest"]) > as_tuple(VERSION) and info["url"].startswith("http")
    except Exception:
        pass
    update_cache = info
    return info


# ---------------------------------------------------------------- permissions

def can_read(path, seconds=2):
    """Can we list this folder? Never waits on a dialog for long."""
    return without_hanging(lambda: bool(os.listdir(path) is not None), seconds) is not None


def permissions():
    """Every folder Skurra would like, and what was decided about each.

    This deliberately touches nothing. Reading a guarded folder is exactly
    what makes macOS put its dialog up, so asking "may we?" used to demand
    an answer before the person had seen anything. We report what we were
    told last time and leave the asking to them.
    """
    state = load_perms()
    out = []
    for r in disk_roots():
        if r == HOME:
            continue
        remembered = state["folders"].get(str(r), "unknown")
        if (not WINDOWS and remembered == "granted"
                and state["grant_versions"].get(str(r)) != VERSION):
            remembered = "unknown"
        out.append({"name": r.name, "path": str(r),
                    "why": WHY_FOLDER.get(r.name, "Measures what is in here."),
                    "state": remembered})
    # Skurra is ad-hoc signed, so every build is a different app to macOS and
    # an update wipes the folder grants. A decision made by an older version
    # no longer describes reality, so say so and let the page ask again.
    return {"folders": out, "asked": state["asked"], "mac": not WINDOWS,
            "stale": bool(state["asked"] and state["version"] != VERSION)}


# One Mac folder question at a time. A second knock while the first dialog
# is up is what leaves OK sitting there doing nothing.
_perm_lock = threading.Lock()


def ask_permission(path):
    """Touch the folder so the system puts its dialog up, and wait for an answer.

    The dialog belongs to the system, not to us: all we can do is knock and
    give the person time to answer. A dialog that is still on screen is not a
    no. Recording that timeout as declined is how Allow selected walked on to
    the next folder while the first question was still waiting.

    On a Mac that knock has to happen on the main thread. Asked from the
    request thread, the dialog appears and OK never gets through.
    """
    if not _perm_lock.acquire(blocking=False):
        return {"ok": False, "pending": True}

    outcome = {}
    done = threading.Event()

    def run():
        try:
            # The question is a Mac system dialog. It only accepts OK when the
            # app is in front and this call is on the main thread.
            if not WINDOWS:
                try:
                    from AppKit import NSApplication
                    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                except Exception:
                    pass
            os.listdir(path)
            outcome["got"] = True
        except OSError:
            outcome["got"] = False
        finally:
            done.set()
            _perm_lock.release()

    scheduled = False
    if not WINDOWS and threading.current_thread() is not threading.main_thread():
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(run)
            scheduled = True
        except Exception:
            scheduled = False
    if not scheduled:
        run()

    if not done.wait(20):
        return {"ok": False, "pending": True}

    got = outcome["got"]
    if got:                                   # they said yes: forget we ever failed
        known = load_guarded()
        for gone in [g for g in known if g == str(path) or g.startswith(str(path) + os.sep)]:
            known.discard(gone)
        save_guarded(known)
    state = load_perms()
    state["folders"][str(path)] = "granted" if got else "declined"
    versions = dict(state.get("grant_versions") or {})
    if got:
        versions[str(path)] = VERSION
    else:
        versions.pop(str(path), None)
    state["grant_versions"] = versions
    state["asked"] = True
    state["version"] = VERSION
    save_perms(state)
    return {"ok": got}


def open_privacy_settings():
    if WINDOWS:
        return False
    pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    subprocess.run(["open", pane], capture_output=True)
    return True


# ---------------------------------------------------------------- installing an update

installing = {"stage": "", "pct": 0, "error": "", "got": 0, "total": 0, "attempt": 1}

RESULT_FILE = DATA / "update-result.json"


def last_update_result():
    """How the previous swap went, read once and then forgotten."""
    try:
        out = json.loads(RESULT_FILE.read_text())
        RESULT_FILE.unlink()
        return out
    except Exception:
        return None


def ps_quote(text):
    """A PowerShell single-quoted string: only ' needs escaping, and % is inert."""
    return "'" + str(text).replace("'", "''") + "'"


def sh_quote(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


def packaged():
    """True when this is the built app, not app.py run from the source."""
    return getattr(sys, "frozen", False)


def app_path():
    """The thing an update has to replace."""
    exe = Path(sys.executable)
    if WINDOWS:
        return exe                                  # Skurra.exe
    return exe.parents[2]                           # .../Skurra.app


def fetch(url, into, on_bit, tries=3):
    """Download a file, surviving a stall, and refuse a half-finished one.

    A wobbly connection used to end the whole update: one read that took
    too long raised, and there was no second go. Now a stall just means
    reconnecting and carrying on from the byte we reached.
    """
    got, total = 0, 0
    last = ""

    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url)
        if got:
            req.add_header("Range", f"bytes={got}-")      # carry on, do not restart
        try:
            with urllib.request.urlopen(req, timeout=60, context=HTTPS) as r:
                if not total:
                    size = int(r.headers.get("Content-Length") or 0)
                    total = size + got if r.status == 206 else size
                elif r.status != 206 and got:
                    got = 0                                # server ignored Range: start over
                    into.write_bytes(b"")
                with open(into, "ab" if got else "wb") as f:
                    while True:
                        chunk = r.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        on_bit(int(got / total * 100) if total else 0, got, total, attempt)
            if not total or got >= total:
                break
            last = f"the connection closed after {got} of {total} bytes"
        except Exception as e:
            last = str(e) or e.__class__.__name__
            got = into.stat().st_size if into.exists() else 0
        if attempt < tries:
            on_bit(int(got / total * 100) if total else 0, got, total, attempt + 1)
            time.sleep(1.5)
    else:
        raise OSError(f"the download kept stopping ({last})")

    if total and got != total:
        raise OSError(f"the download stopped early, {got} of {total} bytes")
    if got < 1_000_000:
        raise OSError("that download was too small to be Skurra")
    with open(into, "rb") as f:
        head = f.read(2)
    if WINDOWS and head != b"MZ":              # a real .exe starts MZ
        raise OSError("what came down was not a Windows program")
    return into


def script_parses(path):
    """Ask the shell itself whether this script is even valid.

    A helper with a syntax error dies the instant it starts, and by then
    Skurra has already quit: the app vanishes and nothing brings it back.
    Better to find out while we are still running.
    """
    try:
        if WINDOWS:
            check = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                     f"'{path}',[ref]$null,[ref]$e); if ($e.Count) {{ exit 1 }}")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", check],
                               capture_output=True, timeout=30)
        else:
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True, timeout=30)
        return r.returncode == 0, (r.stderr or b"").decode()[:200]
    except (OSError, subprocess.TimeoutExpired):
        return True, ""            # no checker available: do not block the update


def sweep_leftovers():
    """Clear work folders and helpers left by updates that never finished."""
    tmp = Path(tempfile.gettempdir())
    for pattern in ("skurra-update-*", "skurra-swap-*"):
        for old in tmp.glob(pattern):
            try:
                shutil.rmtree(old) if old.is_dir() else old.unlink()
            except OSError:
                pass


def install_update():
    """Download the new Skurra, then hand over to a script that swaps it in.

    A running program cannot replace its own files, so a small script waits
    for this process to quit, puts the new one in place and starts it again.
    Whatever happens it writes update-result.json, which the next launch
    reads: without that, a swap that went wrong is invisible.
    """
    installing.update(stage="downloading", pct=0, error="", got=0, total=0)

    def bit(pct, got, total, attempt=1):
        installing.update(pct=pct, got=got, total=total, attempt=attempt)

    def note_json(ok, why):
        # Built here, where quoting is sane, and handed to the script whole.
        return json.dumps({"ok": ok, "error": why, "version": want})

    try:
        want = check_update().get("latest") or VERSION
        work = Path(tempfile.mkdtemp(prefix="skurra-update-"))
        url = DOWNLOAD["windows" if WINDOWS else "mac"].format(tag=f"v{want}")
        target = app_path()
        result = str(RESULT_FILE)

        if WINDOWS:
            fresh = fetch(url, work / "Skurra.exe", bit)
            installing.update(stage="installing", pct=100)

            # PowerShell, not a .bat: it can wait properly, retry the copy and
            # say what went wrong. The script lives outside the folder it
            # deletes, or it would hold that folder open and leak it.
            # Both halves of a one-file build: the python child, and the
            # bootloader that spawned it and will tidy up _MEI afterwards.
            pids = [os.getpid()]
            if packaged():
                try:
                    parent = os.getppid()
                    if parent and parent != os.getpid():
                        pids.append(parent)
                except OSError:
                    pass
            old_pids = ", ".join(str(n) for n in pids)
            # A folder build already has python312.dll beside the exe. Copying a
            # new single file over that exe throws the DLL away. Run the download
            # instead. A lone exe can be replaced, because the download is the
            # launcher and their shortcut should become that launcher.
            beside = (target.parent / "python312.dll").is_file() or (target.parent / "_internal" / "python312.dll").is_file()
            replace = "$false" if beside else "$true"
            script = Path(tempfile.gettempdir()) / f"skurra-swap-{os.getpid()}.ps1"
            # PowerShell, not a .bat: it can wait properly, prove the new copy
            # runs, and put the old one back if it does not.
            script.write_text(f"""
$target = {ps_quote(str(target))}
$fresh  = {ps_quote(str(fresh))}
$replace = {replace}
$work   = {ps_quote(str(work))}
$result = {ps_quote(result)}
$backup = "$target.bak"
$started = {ps_quote(str(STARTED_FILE))}
$ok = $false
$why = 'the update did not finish'

function Save-Note {{
  try {{
    $note = @{{ ok = $ok; error = $why; version = {ps_quote(want)} }} | ConvertTo-Json
    New-Item -ItemType Directory -Force -Path (Split-Path $result) | Out-Null
    Set-Content -LiteralPath $result -Value $note -Encoding UTF8
  }} catch {{}}
}}

try {{
  # Wait for BOTH halves of the old copy. A one-file build is two processes:
  # the bootloader, and the python child it spawns. Waiting only on the child
  # was the bug behind "Failed to load Python DLL". The bootloader deletes its
  # _MEI folder after the child is gone, that folder is named _MEI<its pid>,
  # and Windows hands pids straight back out, so the new copy could claim the
  # same name and have its own python312.dll deleted out from under it.
  foreach ($old in @({old_pids})) {{
    try {{ Wait-Process -Id $old -Timeout 90 -ErrorAction SilentlyContinue }} catch {{}}
  }}

  # Then wait until the file itself can be opened for writing, which means
  # every handle is gone, and give the old copy a moment to finish clearing up.
  for ($i = 0; $i -lt 40; $i++) {{
    try {{
      $h = [System.IO.File]::Open($target, 'Open', 'Write', 'None')
      $h.Close(); break
    }} catch {{ Start-Sleep -Milliseconds 500 }}
  }}
  Start-Sleep -Milliseconds 300

  if ($replace) {{
    Copy-Item -LiteralPath $target -Destination $backup -Force -ErrorAction SilentlyContinue
    $copied = $false
    for ($i = 0; $i -lt 10; $i++) {{
      try {{ Copy-Item -LiteralPath $fresh -Destination $target -Force; $copied = $true; break }}
      catch {{ $why = $_.Exception.Message; Start-Sleep -Milliseconds 700 }}
    }}
    $launch = $target
  }} else {{
    $copied = $true
    $launch = $fresh
  }}

  if ($copied) {{
    # Started, but did it actually get anywhere? "The process still exists"
    # is not proof: a bootloader showing "Failed to load Python DLL" sits
    # there looking perfectly alive until someone clicks OK, and the old
    # check called that a success and threw the backup away. So wait for the
    # new copy to say so itself, by writing its version into started.json.
    Remove-Item -LiteralPath $started -Force -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath $launch -PassThru
    $alive = $false
    for ($i = 0; $i -lt 90; $i++) {{
      Start-Sleep -Seconds 1
      if (Test-Path -LiteralPath $started) {{
        try {{
          # An exact substring, not -match: a regex would read 1.18.0 as
          # "1 anything 18 anything 0" and accept a version that is not ours.
          if ((Get-Content -LiteralPath $started -Raw).Contains({ps_quote(chr(34) + "version" + chr(34) + ": " + chr(34) + want + chr(34))})) {{ $alive = $true; break }}
        }} catch {{}}
      }}
      if ($p -and $p.HasExited -and $i -gt 3) {{ break }}
    }}
    if ($alive) {{
      $ok = $true; $why = ''
      Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }} else {{
      $why = 'the new version would not start, so the old one was put back'
      try {{ if ($p -and -not $p.HasExited) {{ Stop-Process -Id $p.Id -Force }} }} catch {{}}
      if (Test-Path -LiteralPath $backup) {{
        Copy-Item -LiteralPath $backup -Destination $target -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
      }}
    }}
  }}
}} catch {{ $why = $_.Exception.Message }}

Save-Note
# Whatever happened above, the person must end up with a Skurra open.
if (-not $ok) {{
  try {{ Start-Process -FilePath $target }} catch {{}}
}}
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
""", encoding="utf-8")
            launch = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-WindowStyle", "Hidden", "-File", str(script)],
                      {"creationflags": 0x08000000})   # no window, but a console exists
        else:
            dmg = fetch(url, work / "Skurra.dmg", bit)
            installing.update(stage="installing", pct=100)
            mount = work / "mnt"
            mount.mkdir()
            subprocess.run(["hdiutil", "attach", str(dmg), "-mountpoint", str(mount),
                            "-nobrowse", "-quiet"], check=True, timeout=120)
            # One copy, straight to the spot beside the live app. A second
            # copy was a full extra wait before the window could come back.
            beside = Path(str(target) + ".new")
            subprocess.run(["rm", "-rf", str(beside)], timeout=60)
            subprocess.run(["cp", "-R", str(mount / "Skurra.app"), str(beside)],
                           check=True, timeout=300)
            subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], timeout=60)
            if not (beside / "Contents/MacOS/Skurra").exists():
                raise OSError("the downloaded app looks incomplete")

            script = Path(tempfile.gettempdir()) / f"skurra-swap-{os.getpid()}.sh"
            # All that is left after Skurra quits is two renames, which are
            # instant. Then tell Launch Services the bundle changed before
            # asking it to open, or it stalls for half a minute working that
            # out for itself, and confirm the app really did come back.
            script.write_text(f"""#!/bin/bash
target={sh_quote(str(target))}
beside={sh_quote(str(beside))}
work={sh_quote(str(work))}
result={sh_quote(result)}
lsreg=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
for i in $(seq 1 40); do kill -0 {os.getpid()} 2>/dev/null || break; sleep 0.1; done
mkdir -p "$(dirname "$result")"
ok=no
rm -rf "$target.old"
if mv "$target" "$target.old" && mv "$beside" "$target"; then
  ok=yes
  rm -rf "$target.old"
else
  if [ -d "$target.old" ] && [ ! -e "$target" ]; then mv "$target.old" "$target"; fi
  rm -rf "$beside"
fi
back=no
if [ "$ok" = yes ]; then
  # Open the new copy at once. Registering it with Launch Services first
  # used to stall the relaunch for half a minute.
  "$target/Contents/MacOS/Skurra" >/dev/null 2>&1 &
  for i in $(seq 1 25); do
    pgrep -f "$target/Contents/MacOS/" >/dev/null 2>&1 && back=yes && break
    sleep 0.12
  done
  if [ "$back" = no ]; then
    open -n "$target" 2>/dev/null
    for i in $(seq 1 15); do
      pgrep -f "$target/Contents/MacOS/" >/dev/null 2>&1 && back=yes && break
      sleep 0.12
    done
  fi
  "$lsreg" -f "$target" >/dev/null 2>&1 &
fi
if [ "$ok" = yes ] && [ "$back" = yes ]; then
  printf '%s' {sh_quote(note_json(True, ""))} > "$result"
elif [ "$ok" = yes ]; then
  printf '%s' {sh_quote(note_json(False, "The new version was installed but did not reopen on its own"))} > "$result"
else
  printf '%s' {sh_quote(note_json(False, "Skurra could not put the new version in place"))} > "$result"
fi
rm -rf "$work" "$0"
""", encoding="utf-8")
            script.chmod(0o755)
            launch = (["/bin/bash", str(script)], {"start_new_session": True})

        # Two things have to be true before Skurra is allowed to quit: the
        # helper must be a script the shell will accept, and it must actually
        # be running. Quitting into a helper that died is how the app used to
        # disappear and never come back.
        fine, complaint = script_parses(script)
        if not fine:
            raise OSError(f"Skurra wrote an update helper it cannot run: {complaint.strip()}")

        helper = subprocess.Popen(launch[0], **launch[1])
        time.sleep(0.2)
        if helper.poll() is not None:
            raise OSError("the update helper stopped before it could do anything")

        installing.update(stage="restarting")
        threading.Timer(0.25, lambda: os._exit(0)).start()
    except Exception as e:
        installing.update(stage="failed", error=str(e))


# ---------------------------------------------------------------- server

# ---------------------------------------------------------------- long jobs

# Clearing several GB takes longer than the window will wait for a reply. That
# is how "Nothing cleared. Check the Terminal window." came to appear over a
# clear that had worked perfectly: the window hung up, the delete carried on,
# and the page assumed the worst. Nothing slow happens inside a request now.
job = {"running": False, "ready": False, "error": "", "done": 0, "total": 0, "where": ""}
job_lock = threading.Lock()
job_outcome = {}


def start_job(fn):
    """Run fn in its own thread and answer at once."""
    global job_outcome
    if not job_lock.acquire(blocking=False):
        return {"started": False, "running": True}
    job.update(running=True, ready=False, error="", done=0, total=0, where="")
    job_outcome = {}

    def run():
        global job_outcome
        try:
            job_outcome = fn()
            job["error"] = ""
        except Exception as e:
            job_outcome = {}
            job["error"] = str(e) or e.__class__.__name__
        finally:
            job["ready"] = True
            job["running"] = False
            job["where"] = ""
            job_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "running": True}


def job_status():
    return {k: job[k] for k in ("done", "total", "where", "running", "ready", "error")}


def job_report():
    out = dict(job_outcome)
    out["error"] = job["error"]
    return out


def run_sweep(body, wipe, stamp):
    """Clear caches in place, or move things to the Trash. One item at a time,
    saying which, so the page can show real progress instead of a guess."""
    done, refused, failed, partly = 0, [], [], []
    job.update(total=len(body) or 1, done=0)

    for n, it in enumerate(body):
        path = it.get("path", "")
        name = it.get("name", path)
        job.update(done=n, where=name)
        ok, why = allowed(path)
        if ok and wipe and not is_cache(path):
            ok, why = False, "not a cache, it belongs in the Trash"
        if not ok:
            refused.append({"name": name, "why": why})
            record({"at": stamp, "did": "refused", "path": path, "why": why})
            continue
        note = dict(at=stamp, path=path, name=it.get("name"), where=it.get("where"),
                    days=it.get("days"), kind=it.get("kind"), change=it.get("change"))
        if wipe:
            freed, stuck = clear(path)
            known = load_stuck()
            if stuck:
                known.add(path)
            else:
                known.discard(path)
            save_stuck(known)
            note["mb"] = round(freed / 1024 / 1024, 1)
            if stuck == -1:
                failed.append(name)
                note["did"] = "failed"
                note["why"] = "Skurra could not open this"
            elif freed == 0 and stuck:
                failed.append(name)
                note["did"] = "failed"
                note["why"] = "a running program is holding it open"
            elif stuck:
                done += 1
                partly.append(name)
                note["did"] = "partly cleared"
                note["why"] = f"{stuck} still in use"
            else:
                done += 1
                note["did"] = "cleared"
        elif to_trash(path):
            done += 1
            note["did"] = "trashed"
            note["mb"] = it.get("mb")
        else:
            failed.append(name)
            note["did"] = "failed"
            note["mb"] = it.get("mb")
        record(note)

    job.update(done=len(body), where="")
    return {"done": done, "asked": len(body), "refused": refused,
            "failed": failed, "partly": partly, "bin": bin_state()}


def run_binremove(names, stamp):
    job.update(total=len(names) or 1, done=0)
    gone, failed = bin_remove(names)
    for name in names:
        record({"at": stamp, "did": "failed" if name in failed else "deleted for good",
                "name": name, "path": str(TRASH)})
    job.update(done=len(names))
    return {"done": gone, "asked": len(names), "failed": failed, "bin": bin_state()}


def run_empty(stamp):
    job.update(total=1, done=0, where="Trash")
    ok = empty_trash()
    record({"at": stamp, "did": "emptied" if ok else "failed",
            "path": str(TRASH), "name": "Trash"})
    job.update(done=1)
    return {"ok": ok, "bin": bin_state()}


class Handler(SimpleHTTPRequestHandler):

    def reply(self, data):
        body = json.dumps(data).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/api/scan":
            self.reply(start_scan())
        elif self.path == "/api/progress":
            self.reply(scan_status())
        elif self.path == "/api/result":
            self.reply(scan_result())
        elif self.path == "/api/job":
            self.reply(job_status())
        elif self.path == "/api/jobresult":
            self.reply(job_report())
        elif self.path == "/api/moved":
            self.reply(recent())
        elif self.path == "/api/bin":
            self.reply(bin_state())
        elif self.path == "/api/binitems":
            self.reply(bin_items())
        elif self.path == "/api/installing":
            self.reply(installing)
        elif self.path == "/api/lastupdate":
            self.reply(swap_report())
        elif self.path == "/api/permissions":
            self.reply(permissions())
        elif self.path == "/api/update":
            self.reply(check_update())
        elif self.path == "/api/roots":
            self.reply([{"name": "Home" if r == HOME else r.name, "path": str(r)} for r in disk_roots()])
        elif self.path.startswith("/api/search"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.reply(search_system(q.get("q", [""])[0]))
        elif self.path.startswith("/api/thumb"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got = thumb_bytes(q.get("path", [""])[0])
            if not got:
                self.send_error(404)
                return
            body, ctype = got
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path.startswith("/api/explore"):
            q = urllib.parse.urlparse(self.path).query
            parsed = urllib.parse.parse_qs(q)
            want = parsed.get("path", [str(HOME)])[0]
            focus = parsed.get("focus", [""])[0]
            if not can_explore(want):
                self.reply({"error": "outside your home folder", "path": want,
                            "parent": "", "crumbs": [], "items": [], "totals": {}, "mb": 0})
                return
            self.reply(explore_once(want, focus))
        elif self.path == "/api/exploring":
            self.reply(explore_progress)
        else:
            super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "[]")
        stamp = datetime.datetime.now().isoformat(timespec="seconds")

        if self.path == "/api/open":
            url = str(body.get("url", "")) if isinstance(body, dict) else ""
            ok = url.startswith(("http://", "https://"))
            if ok:
                webbrowser.open(url)          # the user's normal browser, not our window
            self.reply({"ok": ok})
            return

        if self.path == "/api/reveal":
            want = str(body.get("path", "")) if isinstance(body, dict) else ""
            self.reply({"ok": reveal(want)})
            return

        if self.path == "/api/permission-ask":
            want = str(body.get("path", "")) if isinstance(body, dict) else ""
            if not in_home_text(want):
                self.reply({"ok": False})
                return
            self.reply(ask_permission(want))
            return

        if self.path == "/api/permissions-done":
            # Closing without granting means exactly that: anything not
            # allowed is written down as declined, so no later scan wanders
            # into it and lets macOS ask on Skurra's behalf.
            state = load_perms()
            for r in disk_roots():
                if r == HOME:
                    continue
                if state["folders"].get(str(r)) != "granted":
                    state["folders"][str(r)] = "declined"
            state["asked"] = True
            state["version"] = VERSION
            save_perms(state)
            self.reply({"ok": True})
            return

        if self.path == "/api/open-privacy":
            self.reply({"ok": open_privacy_settings()})
            return

        if self.path == "/api/install-update":
            if not packaged():
                self.reply({"ok": False,
                            "why": "This copy runs from the source. Pull the code instead."})
                return
            info = check_update()
            if not info["available"]:
                self.reply({"ok": False, "why": "You are already on the newest version."})
                return
            threading.Thread(target=install_update, daemon=True).start()
            self.reply({"ok": True})
            return

        if self.path == "/api/binremove":
            names = [str(x) for x in body] if isinstance(body, list) else []
            self.reply(start_job(lambda: run_binremove(names, stamp)))
            return

        if self.path == "/api/empty":
            self.reply(start_job(lambda: run_empty(stamp)))
            return

        if self.path not in ("/api/trash", "/api/clear"):
            self.send_error(404)
            return

        wipe = self.path == "/api/clear"
        self.reply(start_job(lambda: run_sweep(body, wipe, stamp)))

    def log_message(self, *a):
        pass


def stop_older_skurra():
    """If a previous app.py is still holding our port, stop it first."""
    if WINDOWS:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            bits = line.split()
            if len(bits) >= 5 and bits[1].endswith(f":{PORT}") and bits[3] == "LISTENING":
                pid = int(bits[4])
                if pid != os.getpid():
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                    print("Stopped the Skurra that was already running.")
                    time.sleep(0.6)
        return
    r = subprocess.run(["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    for pid in r.stdout.split():
        pid = int(pid)
        if pid == os.getpid():
            continue
        who = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
        if "app.py" not in who and "Skurra.app" not in who:
            print(f"Something else is using port {PORT}. Change PORT at the top of app.py.")
            raise SystemExit(1)
        os.kill(pid, signal.SIGTERM)
        print("Stopped the Skurra that was already running in another tab.")
        time.sleep(0.6)


stop_older_skurra()
sweep_leftovers()      # clear anything a failed update left behind
server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(HERE)))
threading.Thread(target=server.serve_forever, daemon=True).start()

# The server is up and answering: this build works. Tell the updater.
say_started()

try:
    import webview
except ImportError:
    webview = None

def show_skurra_icon():
    """Keep the dock picture as Skurra. A Python picture makes it look like another app opened."""
    icon = HERE / "icon" / "Skurra.icns"
    if not icon.is_file():
        return
    try:
        from AppKit import NSApplication, NSImage, NSApplicationActivationPolicyRegular
    except ImportError:
        return
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    image = NSImage.alloc().initWithContentsOfFile_(str(icon))
    if image is not None:
        app.setApplicationIconImage_(image)


if webview and not os.environ.get("SKURRA_BROWSER"):
    print("\nSkurra is open in its own window. Close the window to stop.\n")
    show_skurra_icon()
    webview.create_window("Skurra", f"http://localhost:{PORT}",
                          width=1040, height=820, min_size=(720, 560))
    try:
        webview.start()
    except KeyboardInterrupt:
        pass
else:
    print(f"\nRunning. Open http://localhost:{PORT}")
    print("Press Control + C here when you're done.\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass

server.shutdown()
print("Stopped.")
