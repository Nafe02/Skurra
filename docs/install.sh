#!/bin/bash
# Installs Skurra on a Mac in one line:
#   curl -fsSL https://nafe02.github.io/Skurra/install.sh | bash
#
# Downloads the newest Skurra.dmg from GitHub, copies Skurra.app into your
# Applications folder, and opens it. Nothing else is touched.
set -e

DMG_URL="https://github.com/nafe02/Skurra/releases/latest/download/Skurra.dmg"
TMP="$(mktemp -d)"
trap 'hdiutil detach "$TMP/mnt" -quiet 2>/dev/null; rm -rf "$TMP"' EXIT

echo
echo "Skurra — downloading the newest version..."
curl -fL --progress-bar "$DMG_URL" -o "$TMP/Skurra.dmg"

echo "Opening the disk image..."
mkdir -p "$TMP/mnt"
hdiutil attach "$TMP/Skurra.dmg" -mountpoint "$TMP/mnt" -nobrowse -quiet

# Prefer /Applications; fall back to ~/Applications if that is not writable.
DEST="/Applications"
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }

if [ -d "$DEST/Skurra.app" ]; then
  echo "Replacing the Skurra already in $DEST..."
  pkill -f "Skurra.app/Contents/MacOS/Skurra" 2>/dev/null || true
  rm -rf "$DEST/Skurra.app"
fi
echo "Copying Skurra into $DEST..."
cp -R "$TMP/mnt/Skurra.app" "$DEST/"

echo "Done. Opening Skurra."
open "$DEST/Skurra.app"
echo
echo "Skurra is in $DEST. You can launch it from there or from Spotlight any time."
echo
