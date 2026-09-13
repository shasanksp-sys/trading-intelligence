#!/bin/bash
# AppIcon.icns now ships PRE-BUILT inside TradingIntelligence.app, so this
# script is no longer a required setup step. It used to be: the old
# package only shipped the AppIcon.iconset folder of loose PNGs and
# expected you to run this once on the Mac to produce AppIcon.icns via
# iconutil -- if that step was ever skipped or failed silently, the app
# had no real icon and macOS fell back to a generic one, which is why the
# icon looked "unchanged" no matter what image was dropped in.
#
# You only need to run this again if you replace the PNGs inside
# AppIcon.iconset yourself and want to rebuild AppIcon.icns from them.

cd "$(dirname "$0")/TradingIntelligence.app/Contents/Resources" || exit 1

if [ -f "AppIcon.icns" ] && [ ! -d "AppIcon.iconset" ]; then
    echo "AppIcon.icns is already present and there's no iconset folder to"
    echo "rebuild it from -- nothing to do."
    read -p "Press Enter to close."
    exit 0
fi

if [ ! -d "AppIcon.iconset" ]; then
    echo "AppIcon.iconset not found -- nothing to build."
    read -p "Press Enter to close."
    exit 1
fi

iconutil -c icns AppIcon.iconset -o AppIcon.icns

if [ -f "AppIcon.icns" ]; then
    echo "Icon rebuilt successfully: AppIcon.icns"
    # Refresh Finder/Dock's icon cache so the new icon shows up immediately
    touch "$(dirname "$0")/../../.."
    killall Finder 2>/dev/null
    killall Dock 2>/dev/null
else
    echo "Something went wrong -- AppIcon.icns was not created."
fi

echo ""
echo "Press Enter to close this window."
read
