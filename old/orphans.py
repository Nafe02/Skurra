import os
import plistlib
from pathlib import Path

HOME = Path.home()

APP_FOLDERS = [
    Path("/Applications"),
    HOME / "Applications",
    Path("/System/Applications"),
]

LEFTOVER_FOLDERS = [
    HOME / "Library/Application Support",
    HOME / "Library/Caches",
    HOME / "Library/Preferences",
    HOME / "Library/Saved Application State",
]

def installed_apps():
    """Walk the app folders and collect every app's id and name."""
    ids, names = set(), set()
    for folder in APP_FOLDERS:
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

def has_an_owner(entry, ids, names):
    stem = entry.lower()
    for tail in (".plist", ".savedstate", ".binarycookies"):
        if stem.endswith(tail):
            stem = stem[: -len(tail)]
    if stem in names or stem in ids:
        return True
    for bid in ids:                 # com.webcatalog.jordan.ShipIt
        if stem.startswith(bid + "."):
            return True
    return False

ids, names = installed_apps()
print(f"\nFound {len(ids)} installed apps on this Mac.")

found = []
for folder in LEFTOVER_FOLDERS:
    if not folder.exists():
        continue
    for item in folder.iterdir():
        if item.name.lower().startswith("com.apple."):
            continue            # macOS itself, leave it alone
        if has_an_owner(item.name, ids, names):
            continue
        found.append((size_of(item), folder.name, item.name))

found.sort(reverse=True)
total = sum(size for size, where, name in found)

print(f"{len(found)} items match no installed app. {round(total / 1024 / 1024)} MB total.\n")
for size, where, name in found[:25]:
    print(f"{round(size / 1024 / 1024, 1):>9} MB  [{where}]  {name}")

print("\nLOOKS ONLY. Nothing was touched. Review every line before acting.\n")