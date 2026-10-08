#!/usr/bin/env bash
# Start CYBERDECK full screen.
#
#  * From the text console (what the Pi shows at boot after running
#    install-autostart.sh): runs inside a tiny kiosk (cage + foot) that fills
#    the whole display with no desktop, taskbar or window borders.
#  * From a desktop terminal or over SSH: just runs in that terminal.
#
# Extra arguments are passed to cyberdeck.py, e.g.
#     bash launch-cyberdeck.sh --saver 10
#
# Text size in kiosk mode: bigger number = bigger text, fewer columns.
# Change it here, or set CYBERDECK_FONT_SIZE before launching.
FONT_SIZE="${CYBERDECK_FONT_SIZE:-12}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$DIR/cyberdeck.py"

run_here() {
    cd "$DIR" || exit 1
    exec python3 "$APP" "$@"
}

# Already inside a desktop session or an SSH login: use this terminal.
if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}${SSH_CONNECTION:-}" ]; then
    run_here "$@"
fi

# On the bare console: full-screen kiosk if it's installed.
if command -v cage >/dev/null 2>&1 && command -v foot >/dev/null 2>&1; then
    started=$(date +%s)
    cage -s -- foot \
        --term=xterm-256color \
        --font="monospace:size=${FONT_SIZE}" \
        --working-directory="$DIR" \
        -o colors.background=000000 \
        -o colors.foreground=e0e0e0 \
        python3 "$APP" "$@"
    status=$?
    # Normal exit (Q, or it ran for a while): we're done.
    if [ "$status" -eq 0 ] || [ $(( $(date +%s) - started )) -gt 5 ]; then
        exit "$status"
    fi
    echo "Full-screen kiosk could not start (exit $status), so running on the text console instead."
    sleep 2
fi

# Fallback: straight on the text console (fewer colours, but it works).
run_here "$@"
