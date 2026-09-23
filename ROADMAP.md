# Skurra roadmap

Updated 2026-09-23. Android and iPhone stages were dropped: Android 16 shut off
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
- First Skurra.exe sent to a friend on Windows                      done
- Mac build made universal (Intel + Apple chips) after a tester's
  "can't be opened" on an Apple-chip Mac                          done
- Download page with two buttons, GitHub builds both files          done
- First release v1.0.0 published: Skurra.dmg + Skurra.exe          done
- Download page live at nafe02.github.io/Skurra: sticky nav, live
  disk chart, picks Mac or Windows button for the visitor           done
- Windows tester feedback, fix, rebuild, resend                     ongoing
- Disk space view: browse any folder in Home, see what is inside by
  kind, drill in, tick files, send them to the Trash                 done
- Keep testing on both platforms                                    <- you are here

## Costs that never go away
- Mac notarization: $99/year, else users right-click > Open once
- Windows code signing: optional, else SmartScreen warns once
- Everything else (VS Code, Python, PyInstaller, GitHub, GitHub Actions) is free

## Where people get it
- Download page:  https://nafe02.github.io/Skurra/   (docs/index.html)
- Mac download:   https://github.com/nafe02/Skurra/releases/latest/download/Skurra.dmg
- Windows:        https://github.com/nafe02/Skurra/releases/latest/download/Skurra.exe
These links never change. They always hand out the newest release.

## How to ship an update
1. Change `VERSION` at the top of app.py, e.g. "1.1.0"
2. Edit latest.json: same version, one line of notes
3. GitHub Desktop: Commit, then Push
4. GitHub -> Actions -> "Build and release" -> Run workflow
   (about 6 minutes: builds Skurra.dmg and Skurra.exe, publishes them)
5. Every running Skurra shows the update banner on its next launch
