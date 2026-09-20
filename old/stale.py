import os
import time
from pathlib import Path

HOME = Path.home()

# The two dials. These are the whole product.
MIN_MB   = 20    # ignore anything smaller than this
MIN_DAYS = 60    # ignore anything used more recently than this

FOLDERS = [
    HOME / "Library/Application Support",
    HOME / "Library/Caches",
]

def scan(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            return 0, 0
    total, newest = 0, 0
    for root, dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
                t = os.path.getmtime(full)
                if t > newest:
                    newest = t
            except OSError:
                pass
    return total, newest

now = time.time()
rows = []

for folder in FOLDERS:
    if not folder.exists():
        continue
    for item in folder.iterdir():
        if item.name.lower().startswith("com.apple."):
            continue
        size, touched = scan(item)
        mb = size / 1024 / 1024
        days = int((now - touched) / 86400) if touched else 9999
        if mb >= MIN_MB and days >= MIN_DAYS:
            rows.append((size, days, folder.name, item.name))

rows.sort(reverse=True)
total = sum(size for size, d, w, n in rows)

print(f"\nOver {MIN_MB} MB and untouched for {MIN_DAYS}+ days.\n")
for size, days, where, name in rows:
    mb = round(size / 1024 / 1024, 1)
    print(f"{mb:>8} MB   {days:>4} days ago   [{where}]  {name}")

print(f"\n{len(rows)} items, {round(total / 1024 / 1024)} MB total.")
print("LOOKS ONLY. Nothing was touched.\n")