#!/bin/zsh
# Builds Skurra.app and Skurra.dmg into the dist/ folder.  Run with:  zsh build.sh
cd "$(dirname "$0")"
rm -rf build dist
pyinstaller --noconfirm --windowed --name Skurra --target-arch universal2 \
  --icon icon/Skurra.icns \
  --osx-bundle-identifier com.skurra.app \
  --add-data "index.html:." \
  app.py || exit 1
rm -rf build Skurra.spec dist/Skurra

# The disk image: the app plus a shortcut to Applications to drag it into.
rm -rf dist/dmg
mkdir dist/dmg
cp -R dist/Skurra.app dist/dmg/
ln -s /Applications dist/dmg/Applications
hdiutil create -volname "Skurra" -srcfolder dist/dmg -ov -format UDZO -quiet dist/Skurra.dmg
rm -rf dist/dmg

echo
echo "Done."
echo "  App:         $(pwd)/dist/Skurra.app"
echo "  Disk image:  $(pwd)/dist/Skurra.dmg   ($(du -h dist/Skurra.dmg | cut -f1))"
