#!/bin/bash
# Recapture docs/screenshots/app.png from the installed app, at a fixed window size so the README
# image stays consistent.
#
# Needs two macOS permissions for the TERMINAL running this (not for the app):
#   Accessibility    - to move and size the window
#   Screen Recording - for screencapture to see anything at all
set -uo pipefail

APP="/Applications/Disk Usage.app"
PROC="Disk Usage"
OUT="$(cd "$(dirname "$0")/.." && pwd)/docs/screenshots"
SHOT_W=1280
SHOT_H=820
WAIT="${1:-8}"   # seconds to let the rescan finish before capturing

mkdir -p "$OUT"
open -a "$APP" || { echo "Could not open $APP. Run 'make install-app' first."; exit 1; }
sleep 2

geometry () {
    osascript -e "tell application \"System Events\" to tell process \"$PROC\" to get {position, size} of window 1" 2>/dev/null
}

set_geometry () {
    osascript >/dev/null 2>&1 <<OSA
tell application "System Events" to tell process "$PROC"
    set frontmost to true
    set position of window 1 to {$1, $2}
    set size of window 1 to {$3, $4}
end tell
OSA
}

G=$(geometry)
if [ -z "$G" ]; then
    echo "Cannot read the app window. Grant this terminal Accessibility permission:"
    echo "  System Settings > Privacy & Security > Accessibility"
    exit 1
fi
OLD_X=$(echo "$G" | cut -d, -f1 | tr -d ' ')
OLD_Y=$(echo "$G" | cut -d, -f2 | tr -d ' ')
OLD_W=$(echo "$G" | cut -d, -f3 | tr -d ' ')
OLD_H=$(echo "$G" | cut -d, -f4 | tr -d ' ')
trap 'set_geometry "$OLD_X" "$OLD_Y" "$OLD_W" "$OLD_H"' EXIT

set_geometry 80 80 "$SHOT_W" "$SHOT_H"
sleep "$WAIT"
G=$(geometry)
X=$(echo "$G" | cut -d, -f1 | tr -d ' ')
Y=$(echo "$G" | cut -d, -f2 | tr -d ' ')
W=$(echo "$G" | cut -d, -f3 | tr -d ' ')
H=$(echo "$G" | cut -d, -f4 | tr -d ' ')

if ! screencapture -x -R "$X,$Y,$W,$H" "$OUT/app.png" 2>/dev/null || [ ! -s "$OUT/app.png" ]; then
    rm -f "$OUT/app.png"
    echo "screencapture was blocked. Grant this terminal Screen Recording permission:"
    echo "  System Settings > Privacy & Security > Screen Recording, then restart the terminal"
    exit 1
fi
echo "wrote docs/screenshots/app.png"
