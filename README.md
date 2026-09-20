# Skurra

A small storage cleaner for Mac and Windows. It finds caches, app data nobody
has touched in months, and installed apps you no longer use, and lets you
tick what to remove. Nothing is deleted outright: apps and app data go to the
Trash (Recycle Bin on Windows) first, caches are cleared in place, and every
action is written to a diary you can read inside the app.

## Run from source

    pip3 install -r requirements.txt
    python3 app.py

## Build the Mac app and .dmg

    zsh build.sh

## Build the Windows .exe

Push a version tag and GitHub builds it: see `.github/workflows/build.yml`.

## Ship an update

See ROADMAP.md, "How to ship an update".
