"""
Skurra — local disk scanner.
Run with:  python3 app.py
Stop with: close the window (or Control + C in the Terminal)

Opens in its own window when pywebview is installed (pip3 install pywebview),
otherwise falls back to your browser. Force the browser with SKURRA_BROWSER=1.
"""

import datetime
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
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
VERSION = "1.1.0"

# Where Skurra looks for news of a newer version: a small JSON file like
#   {"version": "1.1.0", "url": "https://.../Skurra.dmg", "notes": "What changed"}
# Point this at your own GitHub once you have one.
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

FOLDERS = APPDATA_DIRS + [CACHES_DIR]

# ---------------------------------------------------------------- disk view

# The places worth looking through. Everything here belongs to the user.
def disk_roots():
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music"]
    if WINDOWS:
        names = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
    out = [HOME / n for n in names if (HOME / n).is_dir()]
    return [HOME] + out

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

def is_ours(path):
    low = str(path).lower()
    if any(low == o or low.startswith(o + os.sep) for o in OWN):
        return True
    return Path(path).name.lower().startswith("_mei")


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

progress = {"done": 0, "total": 0}

# Only one scan at a time. A second request (another tab, a refresh) waits
# for the running one and gets its result instead of clobbering progress.
scan_lock = threading.Lock()
last_scan = {"items": [], "skipped": []}

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
    for folder in APP_FOLDERS + [Path("/System/Applications")]:
        if not folder.exists():
            continue
        for root, dirs, files in os.walk(folder):
            for d in list(dirs):
                if not d.endswith(".app"):
                    continue
                names.add(d[:-4].lower())
                try:
                    with open(Path(root) / d / "Contents/Info.plist", "rb") as f:
                        bid = plistlib.load(f).get("CFBundleIdentifier")
                        if bid:
                            ids.add(bid.lower())
                except Exception:
                    pass
                dirs.remove(d)      # don't walk inside the app itself
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


def last_used(path):
    """When Spotlight last saw this opened, as a timestamp. 0 if unknown."""
    if WINDOWS:
        return 0
    r = subprocess.run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", str(path)],
                       capture_output=True, text=True)
    raw = r.stdout.strip()
    if not raw or raw == "(null)":
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %I:%M:%S %p %z"):
        try:
            return datetime.datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------- scanning

def measure(path, tick=None, deadline=None):
    """Returns (total bytes, when anything inside was last changed)."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            return 0, 0

    total, newest, since = 0, 0, 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP]   # never enter private folders
        if deadline and time.time() > deadline:
            break
        for name in files:
            full = os.path.join(root, name)
            try:
                chunk = os.path.getsize(full)
                total += chunk
                since += chunk
                t = os.path.getmtime(full)
                if t > newest:
                    newest = t
            except OSError:
                pass
        if tick and since > 4_000_000:
            tick(since)
            since = 0
    if tick and since:
        tick(since)
    return total, newest


def scan():
    now = time.time()
    ids, names = installed_apps()
    stuck_paths = load_stuck()
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
    for folder in APP_FOLDERS:
        if not folder.exists():
            continue
        try:
            apps = sorted(folder.glob("*.app"))
        except OSError:
            continue
        for app in apps:
            if is_apple(app):
                continue
            targets.append((app, "Applications", "app"))

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

    progress["total"] = sum(weights.values()) or 1
    progress["done"] = 0

    for item, where, kind in targets:
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
        else:
            size, touched = measure(item, tick, deadline=time.time() + ITEM_LIMIT)
        progress["done"] += max(0, budget - spent[0])

        if size == 0:
            continue

        if kind == "app":
            touched = last_used(item) or touched
        days = int((now - touched) / 86400) if touched else 9999
        lines.append(f"{stamp},{key},{size},{days}")
        fresh[key] = size

        if kind != "app" and size / 1024 / 1024 < MIN_MB:
            continue

        # how safe is it to remove this?
        level, why, protected = "safe", "", False
        if kind == "idle":
            if item.name.lower() in KEEP_APPDATA:
                level, why, protected = "important", "holds work that cannot be rebuilt", True
            elif days < MIN_DAYS:
                ago_txt = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
                level, why = "important", f"an app used this {ago_txt}"
            elif has_an_owner(item.name, ids, names):
                level, why = "less", f"app still installed, data untouched for {days} days"
            else:
                level, why = "safe", "no installed app owns this any more"
        elif kind == "cache":
            keep, reason = precious_cache(item) if where == "Caches" else (False, "")
            if keep:
                level, why, protected = "important", reason, True
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
            "protected": protected,
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
    found.sort(key=lambda r: r["mb"], reverse=True)
    return {"items": found, "skipped": skipped}


def scan_once():
    global last_scan
    if scan_lock.acquire(blocking=False):
        try:
            last_scan = scan()
            return last_scan
        finally:
            scan_lock.release()
    with scan_lock:
        return last_scan


# ---------------------------------------------------------------- disk walk

explore_progress = {"done": 0, "total": 1, "where": ""}
explore_lock = threading.Lock()


def folder_size(path, deadline=None):
    """Bytes inside a folder, and how many files. Gives up at the deadline."""
    total, count = 0, 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP and not is_ours(os.path.join(root, d))]
        if deadline and time.time() > deadline:
            break
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def explore(path):
    """Everything one level inside a folder, each with its true size."""
    p = Path(path)
    try:
        entries = [e for e in os.scandir(p)
                   if not e.name.startswith(".")
                   and e.name.lower() not in SKIP
                   and not is_ours(e.path)]
    except OSError as e:
        return {"error": str(e), "path": str(p), "items": []}

    explore_progress.update(done=0, total=max(1, len(entries)), where=p.name or str(p))
    items = []
    for n, e in enumerate(entries):
        explore_progress["done"] = n
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        try:
            if is_dir:
                size, files = folder_size(e.path, deadline=time.time() + ITEM_LIMIT)
            else:
                size, files = e.stat(follow_symlinks=False).st_size, 1
            touched = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        items.append({
            "name": e.name,
            "path": e.path,
            "dir": is_dir,
            "kind": kind_of(e.path, is_dir),
            "mb": round(size / 1024 / 1024, 1),
            "files": files,
            "days": int((time.time() - touched) / 86400) if touched else 0,
        })
    explore_progress["done"] = explore_progress["total"]

    items.sort(key=lambda r: r["mb"], reverse=True)
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
            "mb": round(sum(i["mb"] for i in items), 1)}


def explore_once(path):
    if explore_lock.acquire(blocking=False):
        try:
            return explore(path)
        finally:
            explore_lock.release()
    with explore_lock:
        return explore(path)


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

def allowed(path):
    """Returns (yes_or_no, reason). A path has to clear every gate."""
    if is_ours(path):
        return False, "Skurra is running from here"
    if str(path) in uninstallers:
        return True, ""                      # a Windows program, removed by its uninstaller
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "does not exist"

    caches = CACHES_DIR.resolve()
    appdatas = [d.resolve() for d in APPDATA_DIRS]

    if any(p == f.resolve() for f in FOLDERS):
        return False, "that is an entire scan folder"

    if p.parent == caches:
        keep, reason = precious_cache(p)
        if keep:
            return False, reason
        return True, ""

    if p.parent in appdatas:
        if p.name.lower() in KEEP_APPDATA or p.name.lower() in SKIP:
            return False, "holds work that cannot be rebuilt"
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
    if inside_home(p):
        if p in [r.resolve() for r in disk_roots()]:
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
            or any(p == b.resolve() for b in BROWSER_CACHES if b.exists()))


def to_trash(path):
    if str(path) in uninstallers:
        # Windows programs are not files to move; hand over to their uninstaller.
        name, cmd, kb = uninstallers[str(path)]
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

def check_update():
    """Fetch latest.json once per run. Any failure means 'no update'."""
    global update_cache
    if update_cache is not None:
        return update_cache
    info = {"current": VERSION, "latest": VERSION, "available": False, "url": "", "notes": "",
            "os": "windows" if WINDOWS else "mac"}
    try:
        with urllib.request.urlopen(UPDATE_URL, timeout=4) as r:
            latest = json.loads(r.read().decode())
        info["latest"] = str(latest.get("version", VERSION))
        info["url"] = str(latest.get("url", ""))
        info["notes"] = str(latest.get("notes", ""))
        info["available"] = as_tuple(info["latest"]) > as_tuple(VERSION) and info["url"].startswith("http")
    except Exception:
        pass
    update_cache = info
    return info


# ---------------------------------------------------------------- server

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
            self.reply(scan_once())
        elif self.path == "/api/progress":
            self.reply(progress)
        elif self.path == "/api/moved":
            self.reply(recent())
        elif self.path == "/api/bin":
            self.reply(bin_state())
        elif self.path == "/api/update":
            self.reply(check_update())
        elif self.path == "/api/roots":
            self.reply([{"name": "Home" if r == HOME else r.name, "path": str(r)} for r in disk_roots()])
        elif self.path.startswith("/api/explore"):
            q = urllib.parse.urlparse(self.path).query
            want = urllib.parse.parse_qs(q).get("path", [str(HOME)])[0]
            if not (want == str(HOME) or inside_home(want)):
                self.reply({"error": "outside your home folder", "items": []})
                return
            self.reply(explore_once(want))
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

        if self.path == "/api/empty":
            ok = empty_trash()
            record({"at": stamp, "did": "emptied" if ok else "failed",
                    "path": str(TRASH), "name": "Trash"})
            self.reply({"ok": ok, "bin": bin_state()})
            return

        if self.path not in ("/api/trash", "/api/clear"):
            self.send_error(404)
            return

        wipe = self.path == "/api/clear"
        done, refused, failed, partly = 0, [], [], []

        for it in body:
            path = it.get("path", "")
            name = it.get("name", path)
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

        self.reply({"done": done, "asked": len(body), "refused": refused,
                    "failed": failed, "partly": partly, "bin": bin_state()})

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
server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(HERE)))
threading.Thread(target=server.serve_forever, daemon=True).start()

try:
    import webview
except ImportError:
    webview = None

if webview and not os.environ.get("SKURRA_BROWSER"):
    print("\nSkurra is open in its own window. Close the window to stop.\n")
    webview.create_window("Skurra", f"http://localhost:{PORT}",
                          width=840, height=800, min_size=(640, 520))
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
