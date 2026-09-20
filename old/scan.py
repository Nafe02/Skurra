import os
import subprocess
from pathlib import Path

HOME = Path.home()
CACHES = HOME / "Library/Caches"

# Safety switch. Leave False until you have read the SAFE list and mean it.
ACTUALLY_DELETE = False

VERDICTS = {
    # browsers
    "Google":                        (True,  "Chrome cache, rebuilds as you browse"),
    "com.apple.Safari":              (True,  "Safari cache, rebuilds as you browse"),
    "Firefox":                       (True,  "Firefox cache, rebuilds as you browse"),
    # updaters holding installers already applied
    "com.electron.wispr-flow.ShipIt": (True, "Update already installed"),
    "@webcatalogdesktop-updater":    (True,  "Update already installed"),
    "webcatalog":                    (True,  "App cache, rebuilds on use"),
    # developer tools that re-download on demand
    "Homebrew":                      (True,  "Installers already used"),
    "node-gyp":                      (True,  "Node headers, re-downloads"),
    "pip":                           (True,  "Python packages, re-downloads"),
    "typescript":                    (True,  "Rebuilds on next compile"),
    # apple housekeeping
    "GeoServices":                   (True,  "Map tiles, redownload as you browse"),
    "com.apple.helpd":               (True,  "Help documents, rebuilds"),
    "com.apple.appstoreagent":       (True,  "App Store cache, rebuilds"),
    "com.apple.AppleMediaServices":  (True,  "Rebuilds automatically"),
    "com.apple.amsengagementd":      (True,  "Rebuilds automatically"),
    "com.apple.Music":               (True,  "Artwork only, music lives in ~/Music"),
    # hands off
    "com.spotify.client":            (False, "Your offline music lives here"),
    "Steam":                         (False, "Game files, re-downloading costs GBs"),
    "com.apple.Photos":              (False, "Thumbnails, slow to rebuild"),
}

def size_of(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total

def move_to_trash(path):
    """Asks Finder to do it. Goes to Trash, recoverable, undoable with Cmd+Z."""
    script = f'tell application "Finder" to delete POSIX file "{path}"'
    done = subprocess.run(["osascript", "-e", script], capture_output=True)
    return done.returncode == 0

rows = []
for item in CACHES.iterdir():
    rows.append((size_of(item), item.name, item))
rows.sort(reverse=True)

queue = []
reclaimable = 0
unknown = 0

print()
for size, name, path in rows[:20]:
    mb = round(size / 1024 / 1024)
    if mb == 0:
        continue
    safe, why = VERDICTS.get(name, (None, "not reviewed yet"))
    if safe is True:
        tag = "SAFE"
        reclaimable += size
        queue.append((mb, name, path))
    elif safe is False:
        tag = "KEEP"
    else:
        tag = "  ? "
        unknown += size
    print(f"{tag}  {mb:>5} MB   {name}")

print()
print(f"Safe to reclaim: {round(reclaimable / 1024 / 1024)} MB")
print(f"Still unknown:   {round(unknown / 1024 / 1024)} MB")
print()

if ACTUALLY_DELETE:
    print("Moving to Trash. Recoverable from there.")
    for mb, name, path in queue:
        ok = move_to_trash(path)
        print(f"    {'moved ' if ok else 'FAILED'} {mb:>5} MB   {name}")
else:
    print("DRY RUN. Nothing was touched. These would go to Trash:")
    for mb, name, path in queue:
        print(f"    {mb:>5} MB   {path}")
    print()
    print("When ready: change ACTUALLY_DELETE to True at the top.")
print()