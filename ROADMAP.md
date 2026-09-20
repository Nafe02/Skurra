# Skurra roadmap

Updated 2026-09-20. Android and iPhone stages were dropped: Android 16 shut off
cache clearing for third-party apps, and iPhone apps cannot see outside their
own sandbox, so neither can be a real cleaner. Skurra is a desktop app.

## Stage 0 — a script that only looks                      DONE
`old/scan.py`. Reads the junk folders, prints sizes, deletes nothing.

## Stage 1 — the Mac app                                   DONE
- Scan caches, app data and installed apps                          done
- Tell caches from app data; caches clear in place, the rest goes to Trash   done
- Classify app data: safe / less important / important             done
- Tick boxes, Move to Trash, Empty Trash, diary of everything done   done
- Own window (pywebview), Skurra.app (PyInstaller), Skurra.dmg      done
- Update banner: checks latest.json, offers the new download        done
- Use it for two weeks and write down what annoys you              in progress

## Stage 1.5 — Windows (.exe)                              NEXT
- Windows has different junk: %LOCALAPPDATA%, %TEMP%, browser caches,
  the Recycle Bin instead of the Trash, no Finder or mdls
- Same page (index.html), a Windows branch in app.py for the folders and
  the recycle-bin call
- The .exe cannot be built on a Mac. Build it with GitHub Actions (free):
  push the code, GitHub builds Skurra.exe on a Windows machine
- Publish both downloads and latest.json on GitHub Releases

## Costs that never go away
- Mac notarization: $99/year, else users right-click > Open once
- Windows code signing: optional, else SmartScreen warns once
- Everything else (VS Code, Python, PyInstaller, GitHub, GitHub Actions) is free

## How to ship an update
1. Change `VERSION` at the top of app.py, e.g. "1.1.0"
2. `zsh build.sh`  -> dist/Skurra.dmg
3. Upload the .dmg to a GitHub Release
4. Edit latest.json: same version, the download link, one line of notes
5. Commit latest.json. Every running Skurra shows the banner on next launch
