#!/usr/bin/env bash
# One-time setup: boot the Pi straight into CYBERDECK, full screen,
# instead of the desktop.   Run with:   bash ~/Documents/UI/install-autostart.sh
#
# What it does:
#   1. installs cage + foot (a minimal full-screen kiosk, no desktop needed)
#   2. sets the Pi to boot to the console and log you in automatically
#   3. adds a few lines to your login profile that start CYBERDECK on the
#      main screen (never over SSH, never inside the desktop)
#
# Undo everything with:   bash ~/Documents/UI/uninstall-autostart.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH="$DIR/launch-cyberdeck.sh"
BEGIN="# >>> cyberdeck autostart >>>"
END="# <<< cyberdeck autostart <<<"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (not with sudo); it asks for sudo itself."
    exit 1
fi
if [ ! -f "$LAUNCH" ]; then
    echo "Can't find $LAUNCH. Keep this script in the same folder as cyberdeck.py."
    exit 1
fi

echo "== CYBERDECK autostart setup =="

echo "[1/3] Installing the full-screen kiosk (cage + foot)..."
if sudo apt-get update -qq && sudo apt-get install -y cage foot; then
    echo "      done"
else
    echo "      Couldn't install them (no internet?). CYBERDECK will still start,"
    echo "      just on the plain text console with fewer colours. Re-run this"
    echo "      script later to add the kiosk."
fi

echo "[2/3] Booting to the console with automatic login..."
sudo raspi-config nonint do_boot_behaviour B2
echo "      done"

echo "[3/3] Starting CYBERDECK when you log in on the main screen..."
if [ -f "$HOME/.bash_profile" ]; then
    PROFILE="$HOME/.bash_profile"
else
    PROFILE="$HOME/.profile"
fi
touch "$PROFILE"
# drop any older copy of our block, then append the current one
sed -i "/^$BEGIN\$/,/^$END\$/d" "$PROFILE"
cat >> "$PROFILE" <<EOF
$BEGIN
cyberdeck() { bash "$LAUNCH" "\$@"; }
desktop() { sudo systemctl start lightdm; }
if [ -z "\${SSH_CONNECTION:-}" ] && [ -z "\${WAYLAND_DISPLAY:-}\${DISPLAY:-}" ] && [ "\$(tty)" = "/dev/tty1" ]; then
    bash "$LAUNCH"
    echo
    echo "CYBERDECK closed.  Type 'cyberdeck' to start it again, 'desktop' for the"
    echo "desktop, or 'sudo reboot'."
fi
$END
EOF
echo "      added to $PROFILE"

echo
echo "All set. From the next boot the Pi goes straight into CYBERDECK."
echo "  * Q quits to a text prompt; type 'cyberdeck' to restart it or 'desktop' for the desktop."
echo "  * Text too small or big? Edit FONT_SIZE in $LAUNCH"
echo "  * Undo: bash $DIR/uninstall-autostart.sh"
echo
read -r -p "Reboot now? [y/N] " answer
case "$answer" in
    [yY]*) sudo reboot ;;
    *) echo "OK, it will start on the next reboot." ;;
esac
