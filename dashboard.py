import os
import csv
import time
import datetime
import webbrowser
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).parent
HISTORY = Path.home() / "Library/Application Support/Skurra/history.csv"
PAGE = HERE / "dashboard.html"

MIN_MB = 20
MIN_DAYS = 60

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
today = datetime.date.today().isoformat()
current = {}

for folder in FOLDERS:
    if not folder.exists():
        continue
    for item in folder.iterdir():
        if item.name.lower().startswith("com.apple."):
            continue
        size, touched = scan(item)
        if size == 0:
            continue
        days = int((now - touched) / 86400) if touched else 9999
        current[f"{folder.name}/{item.name}"] = (size, days)

# what did we see last time?
previous = {}
if HISTORY.exists():
    with open(HISTORY) as f:
        for row in csv.DictReader(f):
            previous[row["key"]] = int(row["bytes"])

# write today's numbers to the log
first_time = not HISTORY.exists()
with open(HISTORY, "a", newline="") as f:
    w = csv.writer(f)
    if first_time:
        w.writerow(["date", "key", "bytes", "days_idle"])
    for key, (size, days) in current.items():
        w.writerow([today, key, size, days])

rows = []
for key, (size, days) in current.items():
    mb = size / 1024 / 1024
    if mb < MIN_MB or days < MIN_DAYS:
        continue
    before = previous.get(key)
    if before is None:
        change = "new"
    else:
        diff = (size - before) / 1024 / 1024
        change = f"{diff:+.1f} MB" if abs(diff) >= 0.1 else "no change"
    rows.append((size, days, key, change))

rows.sort(reverse=True)
total_mb = round(sum(s for s, d, k, c in rows) / 1024 / 1024)

body = ""
dots = ""
biggest = rows[0][0] if rows else 1

for size, days, key, change in rows:
    where, name = key.split("/", 1)
    fill = max(4, round(size / biggest * 100))
    pos = max(0, min(100, round((1 - min(days, 365) / 365) * 100)))
    dot = 7 + round(min(size / biggest, 1) * 15)

    if change == "new":
        state, word = "fresh", "first scan"
    elif change == "no change":
        state, word = "unchanged", "unchanged"
    elif change.startswith("+"):
        state, word = "active", "grew " + change[1:]
    else:
        state, word = "active", "shrank " + change[1:]

    dots += (f'<i class="dot {state}" style="left:{pos}%;'
             f'width:{dot}px;height:{dot}px" title="{name}"></i>')

    body += f"""
      <div class="row {state}" style="--fill:{fill}%">
        <span class="fill"></span>
        <span class="who"><b>{name}</b><em>{where}</em></span>
        <span class="num">{round(size / 1024 / 1024, 1)} MB</span>
        <span class="age">{days} days</span>
        <span class="state">{word}</span>
      </div>"""

if not rows:
    body = (f'<p class="empty">Nothing over {MIN_MB} MB has gone {MIN_DAYS} days '
            f'untouched. Lower the two numbers at the top of dashboard.py to look deeper.</p>')

if first_time:
    note = "First scan. Run it again in a few days and this page will show what came back."
else:
    still = sum(1 for s, d, k, c in rows if c == "no change")
    note = f"{still} of {len(rows)} unchanged since the last scan."

html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sitting unused</title>
<style>
/* ===================================================================
   YOUR CONTROL PANEL. Change the values here and everything follows.
   =================================================================== */
:root {{
  --paper:   #e4e7e5;   /* page */
  --card:    #f6f8f7;   /* each row */
  --ink:     #14191a;   /* text */
  --dim:     #6d7573;   /* quiet text */
  --rule:    #ccd2cf;   /* lines */
  --patina:  #2f6b5e;   /* the flagged colour */
  --step:    22px;      /* spacing rhythm */
  --round:   10px;      /* corner softness */
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --paper:  #191d1e;
    --card:   #232829;
    --ink:    #e9eeec;
    --dim:    #8d9794;
    --rule:   #333a3b;
    --patina: #6bbfa8;
  }}
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  padding: calc(var(--step) * 2);
  background: var(--paper);
  color: var(--ink);
  font: 400 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums;
}}
main {{ max-width: 680px; margin: 0 auto; }}

h1 {{ margin: 0; font-size: 29px; font-weight: 600; letter-spacing: -0.02em; }}
.note {{ margin: 6px 0 0; color: var(--dim); font-size: 14px; }}

/* ---- the age line ---- */
.axis {{ margin: calc(var(--step) * 2) 0; }}
.track {{
  position: relative; height: 34px;
  border-bottom: 1px solid var(--rule);
}}
.dot {{
  position: absolute; bottom: -1px;
  transform: translate(-50%, 50%);
  border-radius: 50%;
  background: var(--dim);
  border: 2px solid var(--paper);
}}
.dot.unchanged {{ background: var(--patina); }}
.ticks {{
  display: flex; justify-content: space-between;
  margin-top: 9px; color: var(--dim); font-size: 12px;
}}

/* ---- the list ---- */
.row {{
  position: relative; display: flex; align-items: center; gap: 14px;
  padding: 15px 17px; margin-bottom: 5px;
  background: var(--card); border-radius: var(--round);
  overflow: hidden;
}}
.fill {{
  position: absolute; inset: 0 auto 0 0; width: var(--fill);
  background: var(--patina); opacity: 0.09;
}}
.row.active .fill {{ background: var(--dim); opacity: 0.08; }}
.row > span:not(.fill) {{ position: relative; }}

.who {{ flex: 1; min-width: 0; }}
.who b {{ display: block; font-weight: 500; }}
.who em {{ display: block; font-style: normal; color: var(--dim); font-size: 12px; }}

.num {{ font-weight: 500; }}
.age {{ color: var(--dim); font-size: 13px; width: 72px; text-align: right; }}
.state {{
  font-size: 12px; color: var(--dim);
  width: 94px; text-align: right;
}}
.row.unchanged .state {{ color: var(--patina); font-weight: 500; }}

.empty {{ color: var(--dim); padding: var(--step) 0; }}
.foot {{
  margin-top: calc(var(--step) * 1.5); padding-top: var(--step);
  border-top: 1px solid var(--rule);
  color: var(--dim); font-size: 13px;
}}
</style>
</head>
<body>
<main>
  <h1>Sitting unused</h1>
  <p class="note">{total_mb} MB across {len(rows)} folders. {note}</p>

  <div class="axis">
    <div class="track">{dots}</div>
    <div class="ticks"><span>a year ago</span><span>six months</span><span>today</span></div>
  </div>

  {body}

  <p class="foot">Unchanged means the folder is the same size as the last scan, so
  nothing is using it. Anything that grew is still in use. Scanned {today}.</p>
</main>
</body>
</html>"""

PAGE.write_text(html)
print(f"\n{len(rows)} items, {total_mb} MB")
print(f"Logged to {HISTORY.name}")
webbrowser.open(f"file://{PAGE}")