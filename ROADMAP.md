# Skurra roadmap

Updated 2026-09-21. Android and iPhone stages were dropped: Android 16 shut off
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

## Stage 1.5 — Windows (.exe)                              IN PROGRESS
- Windows branch in app.py: %LOCALAPPDATA%, %TEMP%, Recycle Bin,
  installed programs from the registry                              done
- Code on GitHub (github.com/nafe02/Skurra), GitHub Desktop signed in  done
- GitHub Actions builds Skurra.exe on a Windows machine, free       done
- First Skurra.exe built (14 MB, run #1, 2026-09-20)                done
- A friend tests it on real Windows                                 <- you are here
- Fix what he finds, rebuild, resend (repeat until it is boring)    next
- First proper release: tag v1.0.0 -> .dmg + .exe on GitHub Releases,
  latest.json points at it, the update banner goes live             next

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
