#!/usr/bin/env bash
# Undo install-autostart.sh: boot back into the normal desktop.
#   Run with:   bash ~/Documents/UI/uninstall-autostart.sh
# (cage and foot stay installed; remove them with: sudo apt remove cage foot)
set -euo pipefail

BEGIN="# >>> cyberdeck autostart >>>"
END="# <<< cyberdeck autostart <<<"

for f in "$HOME/.bash_profile" "$HOME/.profile"; do
    if [ -f "$f" ] && grep -qF "$BEGIN" "$f"; then
        sed -i "/^$BEGIN\$/,/^$END\$/d" "$f"
        echo "Removed CYBERDECK autostart from $f"
    fi
done

sudo raspi-config nonint do_boot_behaviour B4
echo "The Pi will boot to the desktop again (logged in automatically)."
read -r -p "Reboot now? [y/N] " answer
case "$answer" in
    [yY]*) sudo reboot ;;
    *) echo "OK, the desktop comes back on the next reboot." ;;
esac
