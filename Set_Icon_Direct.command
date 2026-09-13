#!/bin/bash
# AppIcon.icns now ships pre-built inside TradingIntelligence.app, so
# Finder/Dock should show the correct icon automatically -- no setup step
# needed. Only run this script if the icon STILL looks wrong (old/generic)
# after that, which on macOS usually means Finder or Dock's icon cache is
# stale rather than anything actually wrong with the bundle. This script
# both re-applies the icon directly (bypassing the bundle's Info.plist
# lookup entirely) AND clears the known icon caches, which covers both
# possible causes in one pass.

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
ICON_PATH="$PROJECT_DIR/app_icon_1024.png"
APP_PATH="$PROJECT_DIR/TradingIntelligence.app"

if [ ! -f "$ICON_PATH" ]; then
    echo "app_icon_1024.png not found in $PROJECT_DIR -- cannot proceed."
    read -p "Press Enter to close."
    exit 1
fi

if [ ! -d "$APP_PATH" ]; then
    echo "TradingIntelligence.app not found in $PROJECT_DIR -- cannot proceed."
    read -p "Press Enter to close."
    exit 1
fi

echo "Step 1/2: setting the icon directly via macOS's own icon API..."
osascript -l JavaScript -e "
ObjC.import('Cocoa');
var ws = \$.NSWorkspace.sharedWorkspace;
var img = \$.NSImage.alloc.initWithContentsOfFile('$ICON_PATH');
if (img.isNil()) {
    console.log('FAILED: could not load the icon image file.');
} else {
    var ok = ws.setIconForFileOptions(img, '$APP_PATH', 0);
    console.log(ok ? 'SUCCESS: icon set directly via macOS API.' : 'FAILED: setIconForFileOptions returned false.');
}
"

echo ""
echo "Step 2/2: clearing Finder/Dock icon caches and restarting them..."
# Per-user icon cache (safe, no sudo needed)
find "$HOME/Library/Caches" -iname "*iconservices*" -exec rm -rf {} \; 2>/dev/null
find /var/folders -maxdepth 4 -iname "com.apple.dock.iconcache" -delete 2>/dev/null
find /var/folders -maxdepth 4 -iname "com.apple.iconservices*" -exec rm -rf {} \; 2>/dev/null
touch "$APP_PATH"
killall Dock 2>/dev/null
killall Finder 2>/dev/null

echo ""
echo "Done. Check the Dock/Finder now -- if it still looks wrong after a"
echo "few seconds, log out and back in once; that fully resets the cache."
echo ""
read -p "Press Enter to close."
