#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CYBERDECK - a neon terminal command deck for Raspberry Pi.

  * boot sequence with a real hardware POST
  * live dashboard: CPU cores, thermals, memory, disk, network, processes
  * Noctua fan tachometer on GPIO2: big RPM readout, gauge, history graph,
    stall alarm
  * network page with traffic graphs and gateway / uplink / DNS diagnostics
  * process monitor, stopwatch, countdown timer, notes, key generator
  * built-in shell console, event log, matrix screensaver, power menu

Run it in a terminal (80x24 minimum, bigger looks better):

    python3 cyberdeck.py            # real tachometer on GPIO2
    python3 cyberdeck.py --sim      # simulated fan, no hardware needed
    python3 cyberdeck.py --help     # every option

Standard library only, plus lgpio for the tachometer (preinstalled on
Raspberry Pi OS Bookworm). gpiozero is tried as a fallback.
"""

import argparse
import collections
import curses
import datetime
import locale
import math
import os
import pwd
import random
import re
import secrets
import select
import shutil
import signal
import socket
import string
import struct
import subprocess
import tempfile
import termios
import threading
import time
import uuid

# lgpio drops its notification pipes in the working directory unless told
# otherwise - point it somewhere that is always writable.
os.environ.setdefault("LG_WD", tempfile.gettempdir())
os.environ.setdefault("ESCDELAY", "25")

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE = os.path.join(HERE, "deck_notes.txt")
PAGES = ["DASH", "FAN", "NET", "PROC", "TOOLS", "TERM", "PTEST", "SERIAL"]
MIN_W, MIN_H = 40, 12
BOLD = curses.A_BOLD

# BCM GPIO number -> physical header pin
PHYS = {2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23,
        0: 27, 5: 29, 6: 31, 13: 33, 19: 35, 26: 37, 14: 8, 15: 10, 18: 12,
        23: 16, 24: 18, 25: 22, 8: 24, 7: 26, 1: 28, 12: 32, 16: 36,
        20: 38, 21: 40}

THROTTLE_NOW = [(0, "UNDER-VOLT"), (1, "FREQ CAP"), (2, "THROTTLED"), (3, "TEMP LIMIT")]
THROTTLE_PAST = [(16, "under-volt"), (17, "freq cap"), (18, "throttled"), (19, "temp limit")]

PHRASES = ["ICE INTEGRITY 100%", "NEURAL LINK STABLE", "NO INTRUSIONS DETECTED",
           "ENTROPY POOL NOMINAL", "DAEMONS SLEEPING", "AIRFLOW NOMINAL",
           "UPLINK ENCRYPTED", "WATCHDOG ARMED", "CORE SHIELDS UP",
           "SIGNAL CLEAN", "ALL SYSTEMS GREEN"]

# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

# Fonts are pixel bitmaps ('#' = lit). They are drawn with half-block
# characters, so one pixel is one column wide and half a row tall, and can be
# scaled up by any whole number to fill big screens.
DIGITS = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": ("##.", ".#.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    ":": (".", "#", ".", "#", "."),
    ".": (".", ".", ".", ".", "#"),
    "-": ("...", "...", "###", "...", "..."),
    "_": ("...", "...", "...", "...", "..."),   # blank digit cell (gets a ghost "8")
    " ": ("..", "..", "..", "..", ".."),
}

# (column scale, half-row scale) options, biggest first
DIGIT_LADDER = [(2 * s, s) for s in range(8, 0, -1)] + [(1, 1)]
MINI_LADDER = [(s, s) for s in range(4, 0, -1)]
LOGO_LADDER = [(s, 2 * s) for s in range(6, 0, -1)] + [(1, 1)]

LOGO = {
    "C": (" █████", "██    ", "██    ", "██    ", " █████"),
    "Y": ("██  ██", " ████ ", "  ██  ", "  ██  ", "  ██  "),
    "B": ("█████ ", "██  ██", "█████ ", "██  ██", "█████ "),
    "E": ("██████", "██    ", "█████ ", "██    ", "██████"),
    "R": ("█████ ", "██  ██", "█████ ", "██ ██ ", "██  ██"),
    "D": ("█████ ", "██  ██", "██  ██", "██  ██", "█████ "),
    "K": ("██  ██", "██ ██ ", "████  ", "██ ██ ", "██  ██"),
}

ROTOR = [("  │  ", "──●──", "  │  "),
         (" ╲ ╱ ", "  ●  ", " ╱ ╲ ")]

SPARK = "▁▂▃▄▅▆▇█"


LOGO_FONT = {ch: tuple(r.replace("█", "#").replace(" ", ".") for r in rows) for ch, rows in LOGO.items()}
LOGO_FONT[" "] = ("..",) * 5


def pix_size(text, font, sx=1, sy=1, gap=1):
    """(columns, rows) a string occupies at a given scale."""
    glyphs = [font.get(ch, font[" "]) for ch in text]
    w = (sum(len(g[0]) for g in glyphs) + gap * (len(glyphs) - 1)) * sx
    h = (max(len(g) for g in glyphs) * sy + 1) // 2
    return w, h


def pix_render(text, font, sx=1, sy=1, gap=1):
    """Render text in a pixel font -> list of strings made of █ ▀ ▄ and spaces."""
    glyphs = [font.get(ch, font[" "]) for ch in text]
    ph = max(len(g) for g in glyphs)
    tall = []
    for r in range(ph):
        row = []
        for i, g in enumerate(glyphs):
            if i:
                row.extend([False] * gap)
            line = g[r] if r < len(g) else "." * len(g[0])
            row.extend(c == "#" for c in line)
        row = [p for p in row for _ in range(sx)]
        tall.extend([row] * sy)
    if len(tall) % 2:
        tall.append([False] * len(tall[0]))
    return ["".join("█" if a and b else "▀" if a else "▄" if b else " "
                    for a, b in zip(tall[i], tall[i + 1])) for i in range(0, len(tall), 2)]


def pix_fit(text, font, max_w, max_h, ladder, gap=1):
    """Biggest (sx, sy) from the ladder that fits in max_w x max_h, or None."""
    for sx, sy in ladder:
        w, h = pix_size(text, font, sx, sy, gap)
        if w <= max_w and h <= max_h:
            return sx, sy
    return None


def logo_cells(w, h):
    """Biggest CYBERDECK logo that fits the screen.

    Returns (cells, width, height) where cells = [(row, col, char), ...].
    Tries one line and a stacked CYBER / DECK version, at every scale, and
    falls back to plain text on tiny terminals.
    """
    best = None
    for lines in (("CYBERDECK",), ("CYBER", "DECK")):
        for sx, sy in LOGO_LADDER:
            sizes = [pix_size(t, LOGO_FONT, sx, sy) for t in lines]
            tw = max(a for a, _ in sizes)
            th = sum(b for _, b in sizes) + len(lines) - 1
            if tw <= w - 4 and th <= max(3, h // 2):
                score = (sx * sy, len(lines) == 1)
                if best is None or score > best[0]:
                    best = (score, lines, sx, sy, tw, th)
                break
    if best is None:
        text = "CYBERDECK" if w >= 11 else "CD"
        return [(0, c, ch) for c, ch in enumerate(text)], len(text), 1
    _, lines, sx, sy, tw, th = best
    cells, row = [], 0
    for t in lines:
        rows = pix_render(t, LOGO_FONT, sx, sy)
        off = (tw - len(rows[0])) // 2
        for r, line in enumerate(rows):
            for c, ch in enumerate(line):
                if ch != " ":
                    cells.append((row + r, off + c, ch))
        row += len(rows) + 1
    return cells, tw, th


def alloc(total, items):
    """Share `total` rows between panels.

    items: [(min_rows, wanted_rows, priority, grows), ...] in display order.
    Panels are served in priority order (0 = most important): each gets the
    rows it wants, or whatever is left if that still meets its minimum, so the
    important panels stay complete on small screens. Spare rows go to the last
    panel that grows. Returns a height per item (0 = not shown).
    """
    n = len(items)
    order = sorted(range(n), key=lambda i: items[i][2])
    heights = [0] * n
    left = total
    for i in order:
        mn, want = items[i][0], max(items[i][0], items[i][1])
        if left >= mn:
            heights[i] = min(want, left)
            left -= heights[i]
    if left:
        shown = [i for i in range(n) if heights[i]]
        growers = [i for i in shown if items[i][3]]
        target = growers or shown
        if target:
            heights[target[-1]] += left
    return heights


def is_enter(k):
    return k in ("\n", "\r") or k == curses.KEY_ENTER


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def read_file(path, default=None):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def run_cmd(args, timeout=3.0):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return "?"


def fmt_dur(sec):
    sec = int(max(0, sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    return f"{h:02d}:{m:02d}:{s:02d}"


def clip(text, width):
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def wrap(text, width):
    if width <= 0:
        return []
    if not text:
        return [""]
    return [text[i:i + width] for i in range(0, len(text), width)]


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean_line(s):
    s = ANSI_RE.sub("", s.rstrip("\n"))
    if "\r" in s:
        s = s.split("\r")[-1] or s.replace("\r", "")
    s = s.expandtabs(4)
    return "".join(ch if ch.isprintable() else " " for ch in s)


# --------------------------------------------------------------------------
# fan tachometer
# --------------------------------------------------------------------------

class Tach:
    """Counts falling edges of a fan tach signal and turns them into RPM.

    Noctua fans pull the tach line low twice per revolution (open collector),
    so RPM = pulses_per_second * 60 / 2. GPIO2 has a 1.8k pull-up to 3.3V on
    the Pi board, which is exactly what an open-collector tach needs.
    """

    STALL_SECS = 3.0

    def __init__(self, pin=2, ppr=2, sim=False, debounce_us=0, window=1.5):
        self.pin = pin
        self.ppr = max(1, ppr)
        self.window_s = window
        self.debounce_us = debounce_us
        self.backend = "offline"
        self.detail = ""
        self.error = None
        self.rpm = 0.0
        self.hz = 0.0
        self.stalled = False
        self.history = collections.deque(maxlen=1800)
        self._samples = collections.deque()
        self._zero_since = None
        self._last_hist = 0.0
        self._cleanup = []
        self._count = lambda: 0
        if sim:
            self._init_sim()
        else:
            errors = []
            for name, init in (("lgpio", self._init_lgpio), ("gpiozero", self._init_gpiozero)):
                try:
                    init()
                    break
                except Exception as e:  # noqa: BLE001 - show any failure to the user
                    errors.append(f"{name}: {e}")
                    self._run_cleanup()
            else:
                self.error = "; ".join(errors)
        self.reset_stats()

    # ---- backends ---------------------------------------------------------
    @staticmethod
    def _find_chip(lgpio):
        best = None
        for n in range(16):
            if not os.path.exists(f"/dev/gpiochip{n}"):
                continue
            try:
                h = lgpio.gpiochip_open(n)
            except Exception:  # noqa: BLE001
                continue
            try:
                info = " ".join(str(v) for v in lgpio.gpio_get_chip_info(h))
            except Exception:  # noqa: BLE001
                info = ""
            finally:
                lgpio.gpiochip_close(h)
            if "rp1" in info:            # Raspberry Pi 5 header
                return n
            if "pinctrl-bcm" in info and best is None:   # Pi 4 / 3 / Zero
                best = n
        return best if best is not None else 0

    def _init_lgpio(self):
        import lgpio
        chip = self._find_chip(lgpio)
        h = lgpio.gpiochip_open(chip)
        self._cleanup.append(lambda: lgpio.gpiochip_close(h))
        try:
            lgpio.gpio_claim_alert(h, self.pin, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
        except lgpio.error:
            lgpio.gpio_claim_alert(h, self.pin, lgpio.FALLING_EDGE)
        self._cleanup.append(lambda: lgpio.gpio_free(h, self.pin))
        if self.debounce_us > 0:
            try:
                lgpio.gpio_set_debounce_micros(h, self.pin, self.debounce_us)
            except lgpio.error:
                pass
        cb = lgpio.callback(h, self.pin, lgpio.FALLING_EDGE)   # default = tally counter
        self._cleanup.append(cb.cancel)
        self._count = cb.tally
        self.backend = "lgpio"
        self.detail = f"gpiochip{chip}"

    def _init_gpiozero(self):
        from gpiozero import DigitalInputDevice
        dev = DigitalInputDevice(self.pin, pull_up=True)
        self._cleanup.append(dev.close)
        box = [0]

        def inc():
            box[0] += 1
        dev.when_deactivated = inc
        self._count = lambda: box[0]
        self.backend = "gpiozero"
        self.detail = type(dev.pin_factory).__name__.replace("Factory", "")

    def _init_sim(self):
        state = {"pulses": 0.0, "t": time.monotonic()}

        def count():
            now = time.monotonic()
            dt = now - state["t"]
            state["t"] = now
            rpm = 3150 + 380 * math.sin(now / 9.0) + 140 * math.sin(now / 2.3) + random.uniform(-35, 35)
            state["pulses"] += max(0.0, rpm) * self.ppr / 60.0 * dt
            return int(state["pulses"])
        self._count = count
        self.backend = "sim"
        self.detail = "synthetic signal"

    # ---- runtime ----------------------------------------------------------
    @property
    def online(self):
        return self.backend in ("lgpio", "gpiozero", "sim")

    @property
    def rpm_avg(self):
        return self._sum / self._n if self._n else None

    def reset_stats(self):
        self.rpm_min = None
        self.rpm_max = 0.0
        self._sum = 0.0
        self._n = 0
        try:
            self._c0 = self._count()
        except Exception:  # noqa: BLE001
            self._c0 = 0
        self.total_pulses = 0
        self._warm_until = None      # set on the next update() so stats skip spin-up

    def update(self):
        if not self.online:
            return
        now = time.monotonic()
        try:
            c = self._count()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return
        if self._warm_until is None:
            self._warm_until = now + 2.0
        self.total_pulses = c - self._c0
        self._samples.append((now, c))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        t0, c0 = self._samples[0]
        dt = now - t0
        if dt >= 0.25:
            self.hz = (c - c0) / dt
            self.rpm = self.hz * 60.0 / self.ppr
        elif not self.history:
            return                       # no rate measured yet
        if self.rpm < 1.0:
            if self._zero_since is None:
                self._zero_since = now
            self.stalled = (now - self._zero_since) >= self.STALL_SECS
        else:
            self._zero_since = None
            self.stalled = False
        if now - self._last_hist >= 1.0:
            self._last_hist = now
            self.history.append(self.rpm)
            if now >= self._warm_until:
                self._sum += self.rpm
                self._n += 1
                self.rpm_max = max(self.rpm_max, self.rpm)
                self.rpm_min = self.rpm if self.rpm_min is None else min(self.rpm_min, self.rpm)

    def _run_cleanup(self):
        while self._cleanup:
            fn = self._cleanup.pop()
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        self._run_cleanup()


# --------------------------------------------------------------------------
# system telemetry
# --------------------------------------------------------------------------

class SysMon:
    def __init__(self):
        self.ncpu = os.cpu_count() or 1
        self.hostname = socket.gethostname()
        self.model = (read_file("/proc/device-tree/model", "") or "").strip("\x00\n ") or \
            f"{os.uname().sysname} {os.uname().machine}"
        self.cpu_name = self._cpu_name()
        self.os_name = self._os_name()
        self.kernel = os.uname().release
        self.arch = os.uname().machine
        mf = (read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "") or "").strip()
        self.max_freq = int(mf) // 1000 if mf.isdigit() else None
        self.thermal_path = self._find_thermal()
        self.user = pwd.getpwuid(os.getuid()).pw_name if hasattr(os, "getuid") else "?"

        self.cpu_total = 0.0
        self.cpu_cores = [0.0] * self.ncpu
        self.freqs = [None] * self.ncpu
        self.temp = None
        self.mem = {}
        self.disk = None
        self.uptime = 0.0
        self.load = (0.0, 0.0, 0.0)
        self.net = {}
        self.rx_rate = 0.0
        self.tx_rate = 0.0
        hist = 1800
        self.cpu_hist = collections.deque(maxlen=hist)
        self.temp_hist = collections.deque(maxlen=hist)
        self.rx_hist = collections.deque(maxlen=hist)
        self.tx_hist = collections.deque(maxlen=hist)

        # slow-refresh values (background thread)
        self.ips = {}
        self.ssid = None
        self.wifi = None          # (link quality /70, level dBm)
        self.gateway = None
        self.throttled = None
        self.volts = None

        self._prev_cpu = self._read_cpu()
        self._prev_net = self._read_net()
        self._prev_t = time.monotonic()
        self.refresh_slow()
        self.sample()
        threading.Thread(target=self._slow_loop, daemon=True).start()

    # ---- static info ------------------------------------------------------
    @staticmethod
    def _cpu_name():
        info = read_file("/proc/cpuinfo", "") or ""
        m = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        parts = {"0xd03": "Cortex-A53", "0xd04": "Cortex-A35", "0xd05": "Cortex-A55",
                 "0xd07": "Cortex-A57", "0xd08": "Cortex-A72", "0xd0b": "Cortex-A76"}
        m = re.search(r"^CPU part\s*:\s*(0x[0-9a-fA-F]+)", info, re.M)
        if m:
            return "ARM " + parts.get(m.group(1).lower(), m.group(1))
        return os.uname().machine

    @staticmethod
    def _os_name():
        txt = read_file("/etc/os-release", "") or ""
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', txt, re.M)
        return m.group(1) if m else os.uname().sysname

    @staticmethod
    def _find_thermal():
        base = "/sys/class/thermal"
        try:
            zones = sorted(d for d in os.listdir(base) if d.startswith("thermal_zone"))
        except OSError:
            return None
        for z in zones:
            if "cpu" in (read_file(f"{base}/{z}/type", "") or "").lower():
                return f"{base}/{z}/temp"
        return f"{base}/{zones[0]}/temp" if zones else None

    # ---- raw readers ------------------------------------------------------
    @staticmethod
    def _read_cpu():
        out = {}
        for line in (read_file("/proc/stat", "") or "").splitlines():
            if not line.startswith("cpu"):
                break
            p = line.split()
            try:
                vals = [int(v) for v in p[1:]]
            except ValueError:
                continue
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            out[p[0]] = (idle, sum(vals[:8]))
        return out

    @staticmethod
    def _read_net():
        out = {}
        for line in (read_file("/proc/net/dev", "") or "").splitlines()[2:]:
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            name = name.strip()
            f = data.split()
            if name == "lo" or len(f) < 9:
                continue
            out[name] = (int(f[0]), int(f[8]))
        return out

    @staticmethod
    def _gateway():
        for line in (read_file("/proc/net/route", "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                try:
                    return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
                except (ValueError, struct.error):
                    return None
        return None

    # ---- sampling -----------------------------------------------------------
    def sample(self):
        now = time.monotonic()
        dt = now - self._prev_t
        if dt >= 0.2:
            cpu = self._read_cpu()
            net = self._read_net()

            def usage(name):
                a, b = self._prev_cpu.get(name), cpu.get(name)
                if not a or not b:
                    return 0.0
                di, dtot = b[0] - a[0], b[1] - a[1]
                return max(0.0, min(1.0, 1.0 - di / dtot)) if dtot > 0 else 0.0

            self.cpu_total = usage("cpu")
            self.cpu_cores = [usage(f"cpu{i}") for i in range(self.ncpu)]
            nd, rx_tot, tx_tot = {}, 0.0, 0.0
            for name, (rx, tx) in net.items():
                prx, ptx = self._prev_net.get(name, (rx, tx))
                rxr = max(0.0, (rx - prx) / dt)
                txr = max(0.0, (tx - ptx) / dt)
                nd[name] = {"rx": rx, "tx": tx, "rxr": rxr, "txr": txr}
                rx_tot += rxr
                tx_tot += txr
            self.net = nd
            self.rx_rate, self.tx_rate = rx_tot, tx_tot
            self._prev_cpu, self._prev_net, self._prev_t = cpu, net, now
            self.cpu_hist.append(self.cpu_total)
            self.rx_hist.append(rx_tot)
            self.tx_hist.append(tx_tot)

        if self.thermal_path:
            t = (read_file(self.thermal_path, "") or "").strip()
            if t.lstrip("-").isdigit():
                self.temp = int(t) / 1000.0
                self.temp_hist.append(self.temp)
        freqs = []
        for i in range(self.ncpu):
            f = (read_file(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", "") or "").strip()
            freqs.append(int(f) // 1000 if f.isdigit() else None)
        self.freqs = freqs
        mem = {}
        for line in (read_file("/proc/meminfo", "") or "").splitlines():
            k, _, v = line.partition(":")
            v = v.split()
            if v and v[0].isdigit():
                mem[k] = int(v[0])
        self.mem = mem
        try:
            self.disk = shutil.disk_usage("/")
        except OSError:
            self.disk = None
        up = (read_file("/proc/uptime", "0") or "0").split()
        self.uptime = float(up[0]) if up else 0.0
        try:
            self.load = os.getloadavg()
        except OSError:
            pass

    def refresh_slow(self):
        ips = {}
        out = run_cmd(["ip", "-o", "-4", "addr", "show"])
        if out:
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 4 and p[2] == "inet" and p[1] != "lo":
                    ips.setdefault(p[1], p[3].split("/")[0])
        if not ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("1.1.1.1", 80))    # no packet is sent for UDP connect
                ips["net"] = s.getsockname()[0]
                s.close()
            except OSError:
                pass
        self.ips = ips

        ssid = run_cmd(["iwgetid", "-r"])
        if not ssid:
            out = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show", "--active"])
            if out:
                for line in out.splitlines():
                    name, _, typ = line.rpartition(":")
                    if "wireless" in typ:
                        ssid = name.replace("\\:", ":")
                        break
        self.ssid = ssid or None

        wifi = None
        for line in (read_file("/proc/net/wireless", "") or "").splitlines()[2:]:
            if ":" in line:
                f = line.split(":", 1)[1].split()
                try:
                    wifi = (float(f[1].rstrip(".")), float(f[2].rstrip(".")))
                except (IndexError, ValueError):
                    pass
                break
        self.wifi = wifi
        self.gateway = self._gateway()

        t = run_cmd(["vcgencmd", "get_throttled"])
        if t and "=" in t:
            try:
                self.throttled = int(t.split("=", 1)[1], 16)
            except ValueError:
                pass
        v = run_cmd(["vcgencmd", "measure_volts", "core"])
        if v and "=" in v:
            self.volts = v.split("=", 1)[1]

    def _slow_loop(self):
        while True:
            time.sleep(5)
            try:
                self.refresh_slow()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------

class ProcMon:
    def __init__(self):
        self.clk = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self._prev = {}
        self._prev_t = None
        self._users = {}
        self.rows = []
        self.total = self.running = self.threads = 0

    def _user(self, uid):
        u = self._users.get(uid)
        if u is None:
            try:
                u = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError, ValueError):
                u = str(uid)
            self._users[uid] = u
        return u

    def sample(self, mem_total_kb):
        now = time.monotonic()
        dt = (now - self._prev_t) if self._prev_t else None
        cur, rows, running, threads = {}, [], 0, 0
        mem_total = max(1, mem_total_kb * 1024)
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            st = read_file(f"/proc/{pid}/stat")
            if not st:
                continue
            try:
                lp, rp = st.index("("), st.rindex(")")
                name = st[lp + 1:rp]
                f = st[rp + 2:].split()
                state = f[0]
                ticks = int(f[11]) + int(f[12])
                nthr = int(f[17])
                rss = int(f[21]) * self.page
            except (ValueError, IndexError):
                continue
            ipid = int(pid)
            cur[ipid] = ticks
            cpu = 0.0
            if dt and ipid in self._prev:
                cpu = max(0.0, (ticks - self._prev[ipid]) / self.clk / dt * 100.0)
            try:
                uid = os.stat(f"/proc/{pid}").st_uid
            except OSError:
                uid = -1
            if state == "R":
                running += 1
            threads += nthr
            rows.append((ipid, self._user(uid), cpu, rss / mem_total * 100.0, rss, state, nthr, name))
        self._prev, self._prev_t = cur, now
        self.rows, self.total, self.running, self.threads = rows, len(rows), running, threads

    def top(self, key="cpu", n=20):
        idx = 2 if key == "cpu" else 3
        return sorted(self.rows, key=lambda r: (r[idx], r[4]), reverse=True)[:max(0, n)]


# --------------------------------------------------------------------------
# utilities
# --------------------------------------------------------------------------

class EventLog:
    def __init__(self):
        self.items = collections.deque(maxlen=400)

    def add(self, msg, level="info"):
        self.items.append((time.strftime("%H:%M:%S"), level, msg))


class NetDiag:
    def __init__(self, sysmon, log, host):
        self.sys, self.log, self.host = sysmon, log, host
        self.running = False
        self.results = []
        self.last_run = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.results = []
        self.log.add("net diagnostics started", "info")
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _ping(host, count=3):
        try:
            r = subprocess.run(["ping", "-c", str(count), "-W", "2", host],
                               capture_output=True, text=True, timeout=count * 3 + 4)
        except (OSError, subprocess.SubprocessError) as e:
            return None, None, str(e)
        loss = re.search(r"([\d.]+)% packet loss", r.stdout)
        rtt = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
        return (float(rtt.group(1)) if rtt else None,
                float(loss.group(1)) if loss else None,
                None if (loss or rtt) else (r.stderr.strip() or "no reply"))

    def _ping_result(self, label, host):
        avg, loss, err = self._ping(host)
        if err and avg is None:
            return (label, f"{host}  FAIL  {clip(err, 50)}", "crit")
        if avg is None:
            return (label, f"{host}  NO REPLY  ({loss:.0f}% loss)", "crit")
        lvl = "ok" if (loss or 0) == 0 and avg < 80 else "warn"
        return (label, f"{host}  {avg:.1f} ms  ·  loss {loss:.0f}%", lvl)

    def _run(self):
        try:
            gw = self.sys.gateway
            if gw:
                self.results.append(self._ping_result("GATEWAY", gw))
            else:
                self.results.append(("GATEWAY", "no default route", "crit"))
            self.results.append(self._ping_result("UPLINK", self.host))
            t0 = time.monotonic()
            try:
                addr = socket.getaddrinfo("example.com", 443, socket.AF_INET)[0][4][0]
                ms = (time.monotonic() - t0) * 1000
                self.results.append(("DNS", f"example.com → {addr}  {ms:.0f} ms", "ok" if ms < 300 else "warn"))
            except OSError as e:
                self.results.append(("DNS", f"lookup failed: {e}", "crit"))
            bad = sum(1 for r in self.results if r[2] == "crit")
            self.results.append(("VERDICT", "LINK HEALTHY" if bad == 0 else f"{bad} CHECK(S) FAILED",
                                 "ok" if bad == 0 else "crit"))
            self.log.add("net diagnostics: " + ("all checks passed" if bad == 0 else f"{bad} failed"),
                         "ok" if bad == 0 else "warn")
        finally:
            self.last_run = time.time()
            self.running = False


class Console:
    TIMEOUT = 120

    def __init__(self):
        self.lines = collections.deque(maxlen=2000)
        self.cwd = os.path.expanduser("~")
        self.proc = None
        self.running = False
        self._killing = False
        self.scroll = 0
        self.max_scroll = 0
        self.lines.append(("shell bridge ready · press ! to run a command", "sys"))
        self.lines.append(("cd works; full-screen programs (nano, top) don't · use top -bn1", "sys"))

    def short_cwd(self):
        home = os.path.expanduser("~")
        if self.cwd == home or self.cwd.startswith(home + os.sep):
            return "~" + self.cwd[len(home):]
        return self.cwd

    def clear(self):
        self.lines.clear()
        self.scroll = 0

    def run(self, cmd):
        if self.running:
            self.lines.append(("busy: press K to kill the running command first", "err"))
            return
        self.scroll = 0
        self.lines.append((f"{self.short_cwd()} $ {cmd}", "cmd"))
        m = re.match(r"^cd(?:\s+(.*))?$", cmd.strip())
        if m:
            target = os.path.expanduser((m.group(1) or "~").strip().strip("'\""))
            path = os.path.normpath(os.path.join(self.cwd, target))
            if os.path.isdir(path):
                self.cwd = path
            else:
                self.lines.append((f"cd: no such directory: {target}", "err"))
            return
        self.running = True
        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()

    def _worker(self, cmd):
        killer = None
        try:
            env = dict(os.environ, TERM="dumb", COLUMNS="100", LINES="40",
                       PAGER="cat", SYSTEMD_PAGER="", GIT_PAGER="cat")
            p = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 bufsize=1, start_new_session=True)
            self.proc = p
            killer = threading.Timer(self.TIMEOUT, self.kill, kwargs={"reason": f"timeout {self.TIMEOUT}s"})
            killer.daemon = True
            killer.start()
            for line in p.stdout:
                self.lines.append((clean_line(line), "out"))
            rc = p.wait()
            self.lines.append((f"[exit {rc}]", "sys" if rc == 0 else "err"))
        except Exception as e:  # noqa: BLE001
            self.lines.append((f"error: {e}", "err"))
        finally:
            if killer:
                killer.cancel()
            self.proc = None
            self.running = False
            self._killing = False

    def kill(self, reason="killed"):
        """Actually stop the running command: signal its whole process group,
        escalating SIGINT -> SIGTERM -> SIGKILL until it's really gone."""
        p = self.proc
        if not (p and p.poll() is None) or self._killing:
            return
        self._killing = True
        self.lines.append((f"[{reason} — stopping…]", "err"))

        def reap():
            try:
                pgid = os.getpgid(p.pid)
            except OSError:
                return
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    return                      # group already gone
                for _ in range(8):              # give it ~0.8s to die per signal
                    if p.poll() is not None:
                        return
                    time.sleep(0.1)

        threading.Thread(target=reap, daemon=True).start()


class Stopwatch:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.acc = 0.0
        self.t0 = None
        self.laps = []

    def toggle(self):
        if self.running:
            self.acc += time.monotonic() - self.t0
            self.running = False
        else:
            self.t0 = time.monotonic()
            self.running = True

    def elapsed(self):
        return self.acc + (time.monotonic() - self.t0 if self.running else 0.0)

    def lap(self):
        if self.elapsed() > 0:
            self.laps.append(self.elapsed())


class Timer:
    def __init__(self, minutes=5):
        self.set_s = max(1, min(99, minutes)) * 60
        self.reset()

    def reset(self):
        self.running = False
        self.remaining = float(self.set_s)
        self.end = None
        self.done = False

    def toggle(self):
        if self.running:
            self.remaining = max(0.0, self.end - time.monotonic())
            self.running = False
        else:
            if self.remaining <= 0:
                self.remaining = float(self.set_s)
            self.end = time.monotonic() + self.remaining
            self.running = True
            self.done = False

    def adjust(self, delta):
        if self.running:
            self.end = max(time.monotonic() + 1, self.end + delta)
        else:
            self.set_s = max(60, min(99 * 60, self.set_s + delta))
            self.remaining = float(self.set_s)
            self.done = False

    def left(self):
        return max(0.0, self.end - time.monotonic()) if self.running else self.remaining

    def check(self):
        if self.running and self.left() <= 0:
            self.running = False
            self.remaining = 0.0
            self.done = True
            return True
        return False


class Notes:
    def __init__(self, path):
        self.path = path
        txt = read_file(path, "") or ""
        self.items = [ln for ln in txt.splitlines() if ln.strip()][-200:]

    def add(self, text):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {text}"
        self.items.append(line)
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
            return None
        except OSError as e:
            return str(e)


class KeyGen:
    ALPHA = string.ascii_letters + string.digits + "!@#$%^&*-_=+?"

    def __init__(self):
        self.gen()

    def gen(self):
        self.password = "".join(secrets.choice(self.ALPHA) for _ in range(20))
        self.pin = "".join(secrets.choice(string.digits) for _ in range(6))
        self.hex = secrets.token_hex(8)
        self.uuid = str(uuid.uuid4())
        self.d20 = secrets.randbelow(20) + 1
        self.coin = secrets.choice(("HEADS", "TAILS"))


class Matrix:
    CH = "0123456789ABCDEF<>=+*#$%&@?/\\|"

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.cols = {x: self._new(True) for x in range(0, w, 2)}

    def _new(self, initial=False):
        start = random.randint(-self.h, 0) if initial else random.randint(-self.h // 2, 0)
        return [start, random.choice((1, 1, 1, 2)), random.randint(4, max(5, self.h // 2))]

    def step(self):
        for x, c in self.cols.items():
            c[0] += c[1]
            if c[0] - c[2] > self.h:
                self.cols[x] = self._new()

    def draw(self, P, C):
        for x, (head, _spd, ln) in self.cols.items():
            for i in range(ln):
                y = head - i
                if 0 <= y < self.h:
                    if i == 0:
                        a = C["mxh"] | BOLD
                    elif i < ln // 3:
                        a = C["mx"] | BOLD
                    else:
                        a = C["mx2"]
                    P.put(y, x, random.choice(self.CH), a)


# --------------------------------------------------------------------------
# pentest engines  —  reconnaissance of the OPERATOR'S OWN systems only
# --------------------------------------------------------------------------
# Everything here either reads local state, runs standard tools the user has
# installed, or scans networks the device is itself attached to (RFC1918 /
# link-local, guarded below). There is no exploit, attack or credential code.

AUTHZ = "AUTHORIZED USE ONLY — only assess systems you own or have written permission to test."

_PRIV = [("10.0.0.0", 8), ("172.16.0.0", 12), ("192.168.0.0", 16),
         ("169.254.0.0", 16), ("127.0.0.0", 8)]
COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 139, 143, 443, 445, 548, 631,
                993, 995, 1883, 3306, 3389, 5000, 5353, 5900, 8080, 8443, 9100]


def ip_to_int(ip):
    try:
        a, b, c, d = (int(x) for x in ip.split("."))
    except (ValueError, AttributeError):
        return None
    if min(a, b, c, d) < 0 or max(a, b, c, d) > 255:
        return None
    return (a << 24) | (b << 16) | (c << 8) | d


def is_private_net(cidr):
    """True only if the whole a.b.c.d/pfx network is RFC1918 / link-local / loopback."""
    try:
        base, pfx = cidr.split("/")
        pfx = int(pfx)
    except (ValueError, AttributeError):
        return False
    ni = ip_to_int(base)
    if ni is None:
        return False
    for p, pl in _PRIV:
        if pfx >= pl:
            mask = (0xFFFFFFFF << (32 - pl)) & 0xFFFFFFFF
            if (ni & mask) == (ip_to_int(p) & mask):
                return True
    return False


def own_subnets():
    out = run_cmd(["ip", "-o", "-4", "addr", "show"])
    nets = []
    if out:
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4 and f[2] == "inet" and f[1] != "lo":
                nets.append((f[1], f[3]))
    return nets


def slash24(ip):
    return ip.split("/")[0].rsplit(".", 1)[0] + ".0/24"


class Recon:
    """Host (and optional light port) discovery of the operator's own subnet."""

    def __init__(self, log):
        self.log = log
        self.scanning = False
        self.phase = ""
        self.hosts = []
        self.target = ""
        self.method = ""
        self.error = None
        self.last_run = None
        self.with_ports = False

    def start(self, with_ports=False):
        if self.scanning:
            return
        self.scanning = True
        self.with_ports = with_ports
        self.hosts = []
        self.error = None
        self.phase = "preparing"
        self.log.add(f"recon: {'host+port' if with_ports else 'host'} discovery started", "info")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            priv = [(i, c) for i, c in own_subnets() if is_private_net(c)]
            if not priv:
                self.error = "no private IPv4 subnet on this device"
                return
            ifc, cidr = priv[0]
            try:
                pfx = int(cidr.split("/")[1])
            except (ValueError, IndexError):
                pfx = 24
            scan = cidr if pfx >= 24 else slash24(cidr)
            self.target = f"{scan}  ({ifc})"
            self.hosts = self._discover(scan)
            if self.with_ports and self.hosts:
                self._scan_ports()
            self.log.add(f"recon: {len(self.hosts)} host(s) on {scan}", "ok")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.log.add(f"recon failed: {e}", "crit")
        finally:
            self.phase = ""
            self.scanning = False
            self.last_run = time.time()

    def _discover(self, cidr):
        if shutil.which("nmap"):
            self.method = "nmap -sn"
            self.phase = "ping sweep (nmap)"
            return self._parse_nmap_hosts(run_cmd(["nmap", "-sn", "-n", cidr], timeout=120) or "")
        if shutil.which("arp-scan"):
            self.method = "arp-scan"
            self.phase = "arp sweep (arp-scan)"
            return self._parse_arpscan(run_cmd(["arp-scan", "--numeric", "--retry=1", cidr], timeout=90) or "")
        self.method = "ping sweep (builtin)"
        self.phase = "ping sweep"
        return self._ping_sweep(cidr)

    @staticmethod
    def _parse_nmap_hosts(out):
        hosts, cur = [], None
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r"Nmap scan report for (.+)", line)
            if m:
                tgt = m.group(1)
                mm = re.match(r"(.+) \(([\d.]+)\)$", tgt)
                name, ip = (mm.group(1), mm.group(2)) if mm else ("", tgt)
                cur = {"ip": ip, "name": name if name != ip else "", "mac": "", "vendor": "", "ports": None}
                hosts.append(cur)
            elif line.startswith("MAC Address:") and cur:
                mm = re.match(r"MAC Address: ([0-9A-Fa-f:]+)\s*\(?(.*?)\)?$", line)
                if mm:
                    cur["mac"], cur["vendor"] = mm.group(1), mm.group(2)
        hosts.sort(key=lambda h: ip_to_int(h["ip"]) or 0)
        return hosts

    @staticmethod
    def _parse_arpscan(out):
        hosts = []
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) >= 2 and ip_to_int(f[0]):
                hosts.append({"ip": f[0], "name": "", "mac": f[1],
                              "vendor": f[2] if len(f) > 2 else "", "ports": None})
        hosts.sort(key=lambda h: ip_to_int(h["ip"]) or 0)
        return hosts

    def _ping_sweep(self, cidr):
        base = cidr.split("/")[0].rsplit(".", 1)[0]
        found, lock, batch = [], threading.Lock(), []

        def ping(n):
            ip = f"{base}.{n}"
            r = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                with lock:
                    found.append(ip)

        for n in range(1, 255):
            t = threading.Thread(target=ping, args=(n,), daemon=True)
            t.start()
            batch.append(t)
            if len(batch) >= 64:
                for t in batch:
                    t.join()
                batch = []
        for t in batch:
            t.join()
        arp = {}
        for line in (read_file("/proc/net/arp", "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[3] != "00:00:00:00:00:00":
                arp[f[0]] = f[3]
        found.sort(key=lambda ip: ip_to_int(ip) or 0)
        return [{"ip": ip, "name": "", "mac": arp.get(ip, ""), "vendor": "", "ports": None} for ip in found]

    def _scan_ports(self):
        self.phase = "port scan"
        use_nmap = shutil.which("nmap")
        for h in self.hosts[:32]:
            if use_nmap:
                out = run_cmd(["nmap", "-Pn", "-T4", "-n", "--top-ports", "50", h["ip"]], timeout=60) or ""
                h["ports"] = [int(m.group(1)) for m in re.finditer(r"^(\d+)/tcp\s+open", out, re.M)]
            else:
                h["ports"] = self._connect_scan(h["ip"])

    @staticmethod
    def _connect_scan(ip):
        open_ports = []
        for p in COMMON_PORTS:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.4)
            try:
                if s.connect_ex((ip, p)) == 0:
                    open_ports.append(p)
            except OSError:
                pass
            finally:
                s.close()
        return open_ports


class Wireless:
    """Read-only survey of nearby Wi-Fi access points and Bluetooth devices."""

    def __init__(self, log):
        self.log = log
        self.wifi = []
        self.bt = []
        self.scanning = False
        self.phase = ""
        self.wifi_err = None
        self.bt_err = None
        self.last_run = None

    def start(self):
        if self.scanning:
            return
        self.scanning = True
        self.phase = "wifi"
        self.wifi_err = self.bt_err = None
        self.log.add("wireless survey started", "info")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._scan_wifi()
            self.phase = "bluetooth"
            self._scan_bt()
            self.log.add(f"wireless: {len(self.wifi)} AP(s), {len(self.bt)} BT device(s)", "ok")
        except Exception as e:  # noqa: BLE001
            self.log.add(f"wireless survey failed: {e}", "crit")
        finally:
            self.phase = ""
            self.scanning = False
            self.last_run = time.time()

    def _scan_wifi(self):
        if not shutil.which("nmcli"):
            self.wifi_err = "nmcli not installed  (sudo apt install network-manager)"
            return
        out = run_cmd(["nmcli", "-t", "-f", "SSID,BSSID,CHAN,SIGNAL,SECURITY",
                       "dev", "wifi", "list", "--rescan", "yes"], timeout=25)
        if out is None:
            self.wifi_err = "no Wi-Fi device, or nmcli scan failed"
            return
        aps = []
        for line in out.splitlines():
            parts = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]
            if len(parts) >= 5:
                try:
                    sig = int(parts[3])
                except ValueError:
                    sig = 0
                aps.append({"ssid": parts[0] or "<hidden>", "bssid": parts[1], "chan": parts[2],
                            "signal": sig, "sec": ":".join(parts[4:]) or "open"})
        aps.sort(key=lambda a: -a["signal"])
        self.wifi = aps

    def _scan_bt(self):
        if not shutil.which("bluetoothctl"):
            self.bt_err = "bluetoothctl not installed  (sudo apt install bluez)"
            return
        try:
            subprocess.run(["bluetoothctl", "--timeout", "8", "scan", "on"],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            self.bt_err = f"scan failed: {e}  (is Bluetooth powered on?)"
            return
        out = run_cmd(["bluetoothctl", "devices"], timeout=10) or ""
        devs = []
        for line in out.splitlines():
            m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line.strip())
            if m:
                devs.append({"addr": m.group(1), "name": m.group(2)})
        self.bt = devs
        if not devs:
            self.bt_err = "no devices found (is Bluetooth powered on?)"


KIT_TOOLS = [
    ("nmap", "nmap", "recon", "host/port scanner"),
    ("arp-scan", "arp-scan", "recon", "layer-2 host discovery"),
    ("masscan", "masscan", "recon", "fast port scanner"),
    ("fping", "fping", "recon", "parallel ping sweep"),
    ("tcpdump", "tcpdump", "capture", "packet capture"),
    ("tshark", "tshark", "capture", "CLI wireshark"),
    ("nmcli", "network-manager", "wireless", "wifi control & scan"),
    ("iw", "iw", "wireless", "wifi config / scan"),
    ("aircrack-ng", "aircrack-ng", "wireless", "wifi audit suite"),
    ("bluetoothctl", "bluez", "wireless", "bluetooth control"),
    ("nc", "netcat-openbsd", "net", "netcat"),
    ("socat", "socat", "net", "socket relay"),
    ("curl", "curl", "net", "http client"),
    ("nikto", "nikto", "web", "web server scanner"),
    ("gobuster", "gobuster", "web", "content discovery"),
    ("sqlmap", "sqlmap", "web", "sql-injection tester"),
    ("hydra", "hydra", "audit", "login auditing"),
    ("john", "john", "audit", "password hashes"),
    ("hashcat", "hashcat", "audit", "gpu hashes"),
    ("minicom", "minicom", "serial", "serial terminal"),
    ("screen", "screen", "serial", "serial / mux"),
    ("flashrom", "flashrom", "hardware", "spi flash dumper"),
    ("openocd", "openocd", "hardware", "jtag / swd"),
    ("git", "git", "misc", "version control"),
]


class Kit:
    """Inventory of which recon / audit tools are installed, plus a convenience
    'install all missing' that runs the same `apt install` the panel already
    lists, streaming apt's output into a log."""

    def __init__(self, log):
        self.log = log
        self.rows = []
        self.scanning = False
        self.done = False
        self.n_ok = 0
        self.installing = False
        self.install_log = collections.deque(maxlen=600)

    def start(self):
        if self.scanning:
            return
        self.scanning = True
        self.done = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.refresh()
        self.scanning = False
        self.done = True

    def refresh(self):
        rows = []
        for cmd, pkg, cat, what in KIT_TOOLS:
            rows.append({"cmd": cmd, "pkg": pkg, "cat": cat, "what": what,
                         "path": shutil.which(cmd)})
        self.rows = rows
        self.n_ok = sum(1 for r in rows if r["path"])

    def missing_packages(self):
        seen, out = set(), []
        for r in self.rows:
            if not r["path"] and r["pkg"] not in seen:
                seen.add(r["pkg"])
                out.append(r["pkg"])
        return out

    def install_all(self):
        if self.installing:
            return
        pkgs = self.missing_packages()
        if not pkgs:
            self.install_log.append(("all tools already installed", "ok"))
            return
        self.installing = True
        threading.Thread(target=self._install, args=(pkgs,), daemon=True).start()

    def _ilog(self, text, kind="out"):
        self.install_log.append((clean_line(text), kind))

    def _install(self, pkgs):
        try:
            self._ilog(f"installing {len(pkgs)} package(s): {' '.join(pkgs)}", "sys")
            self.log.add(f"kit: installing {len(pkgs)} package(s)", "info")
            try:
                chk = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                self._ilog(f"cannot run sudo: {e}", "err")
                return
            if chk.returncode != 0:
                self._ilog("passwordless sudo isn't set up, so I can't install from here.", "err")
                self._ilog("run this once in a terminal on the Pi:", "err")
                self._ilog("sudo apt install " + " ".join(pkgs), "cmd")
                self.log.add("kit: install needs a terminal (no passwordless sudo)", "warn")
                return
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            ok = True
            for args, label in ((["sudo", "-n", "apt-get", "update"], "apt-get update"),
                                (["sudo", "-n", "apt-get", "install", "-y"] + pkgs, "apt-get install")):
                self._ilog(f"$ {label}", "cmd")
                try:
                    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, errors="replace", bufsize=1, env=env)
                    for line in p.stdout:
                        self._ilog(line)
                    rc = p.wait()
                except (OSError, subprocess.SubprocessError) as e:
                    self._ilog(f"{label} failed: {e}", "err")
                    ok = False
                    break
                if rc != 0:
                    self._ilog(f"[{label} exited {rc}]", "err")
                    ok = False
                    break
            self.refresh()
            if ok:
                self._ilog(f"done — {self.n_ok}/{len(KIT_TOOLS)} tools now installed", "ok")
                self.log.add(f"kit: install finished, {self.n_ok}/{len(KIT_TOOLS)} ready", "ok")
            else:
                self.log.add("kit: install did not complete (see KIT log)", "warn")
        finally:
            self.installing = False


PENTEST_PHASES = [
    ("1 RECON", "passive + active info gathering: subnets, live hosts, OSINT"),
    ("2 ENUM", "services, versions, shares, users — map the attack surface"),
    ("3 ASSESS", "match findings to known issues (only where authorized)"),
    ("4 POST", "privilege, persistence, pivot — prove the impact"),
    ("5 REPORT", "evidence, impact, remediation — the real deliverable"),
]

PENTEST_GEAR = [
    "USB Ethernet adapter (+ network tap / throwing star)",
    "USB Wi-Fi adapter — monitor mode + injection",
    "Bus Pirate / USB-UART (CP2102, FTDI) — serial, SPI, I2C",
    "8-channel logic analyzer — bus sniffing",
    "RTL-SDR dongle — RF survey",
    "RFID/NFC tool (Proxmark3, PN532)",
    "USB-C hub, OTG adapter, jumper-wire kit",
    "Spare microSD cards + reader",
    "USB-C PD power bank",
    "Pinout + default-credential cheat sheets",
]


# --------------------------------------------------------------------------
# serial console  —  dependency-free terminal for USB serial devices
# --------------------------------------------------------------------------
# Talks to a Bus Pirate, M5Stack Cardputer, Arduino, ESP32, FTDI cable, etc.
# Uses only termios/os/select, so there is nothing to pip install on the Pi.

BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
LINE_ENDINGS = [("CRLF", "\r\n"), ("CR", "\r"), ("LF", "\n"), ("none", "")]


def list_serial_ports():
    out = []
    try:
        entries = os.listdir("/dev")
    except OSError:
        entries = []
    for name in entries:
        if name.startswith(("ttyUSB", "ttyACM", "ttyAMA")):
            out.append("/dev/" + name)
    for extra in ("/dev/serial0", "/dev/serial1"):
        if os.path.exists(extra) and extra not in out:
            out.append(extra)
    return sorted(set(out))


class SerialLink:
    def __init__(self, log, baud=115200):
        self.log = log
        self.baud = baud if baud in BAUDS else 115200
        self.eol_idx = 0
        self.hexmode = False
        self.port = None
        self.fd = -1
        self.connected = False
        self.error = None
        self.lines = collections.deque(maxlen=4000)     # (text, kind) kind in rx/tx/sys
        self._partial = bytearray()
        self._hexbuf = bytearray()
        self.scroll = 0
        self.max_scroll = 0
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.ports = []
        self.sel = 0
        self._last_ports = 0.0
        self._stop = threading.Event()
        self._reader = None
        self._lock = threading.Lock()
        self.refresh_ports(force=True)

    # ---- port list --------------------------------------------------------
    def refresh_ports(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_ports < 1.0:
            return
        self._last_ports = now
        self.ports = list_serial_ports()
        if self.sel >= len(self.ports):
            self.sel = max(0, len(self.ports) - 1)

    def select(self, delta):
        if self.ports:
            self.sel = (self.sel + delta) % len(self.ports)

    @property
    def eol(self):
        return LINE_ENDINGS[self.eol_idx][1]

    @property
    def eol_name(self):
        return LINE_ENDINGS[self.eol_idx][0]

    # ---- connection -------------------------------------------------------
    def _baud_const(self):
        return getattr(termios, f"B{self.baud}", termios.B115200)

    def connect(self, port=None):
        if port is None:
            if not self.ports:
                self._sys("no serial ports detected — plug a device in, then press R")
                return False
            port = self.ports[self.sel]
        self.disconnect(quiet=True)
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            self.error = self._explain(e, port)
            self._sys(self.error)
            self.log.add(f"serial: {self.error}", "crit")
            return False
        try:
            self._configure(fd)
        except (OSError, termios.error) as e:
            os.close(fd)
            self.error = f"{port}: {e}"
            self._sys(self.error)
            return False
        self.fd, self.port, self.connected, self.error = fd, port, True, None
        self._partial = bytearray()
        self._hexbuf = bytearray()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._sys(f"── connected {port} @ {self.baud} 8N1 / {self.eol_name} ──")
        self.log.add(f"serial: connected {port} @ {self.baud}", "ok")
        return True

    def _configure(self, fd):
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(fd)
        b = self._baud_const()
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = (cflag & ~termios.CSIZE) | termios.CS8 | termios.CLOCAL | termios.CREAD
        cflag &= ~(termios.PARENB | termios.CSTOPB)
        if hasattr(termios, "CRTSCTS"):
            cflag &= ~termios.CRTSCTS
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, b, b, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)

    def disconnect(self, quiet=False):
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=0.5)
            self._reader = None
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1
        was = self.connected
        self.connected = False
        if was and not quiet:
            self._sys("── disconnected ──")
            self.log.add("serial: disconnected", "info")
        if not quiet:
            self.port = None

    def cycle_baud(self, delta):
        self.baud = BAUDS[(BAUDS.index(self.baud) + delta) % len(BAUDS)]
        if self.connected and self.port:
            self.connect(self.port)
        else:
            self._sys(f"baud set to {self.baud}")

    def cycle_eol(self):
        self.eol_idx = (self.eol_idx + 1) % len(LINE_ENDINGS)
        self._sys(f"line ending: {self.eol_name}")

    def toggle_hex(self):
        with self._lock:
            self._flush_partial()
            self._hexbuf = bytearray()
        self.hexmode = not self.hexmode
        self._sys("hex view ON" if self.hexmode else "ascii view ON")

    def clear(self):
        self.lines.clear()
        self.scroll = 0

    def _sys(self, msg):
        self.lines.append((msg, "sys"))
        self.scroll = 0

    # ---- i/o --------------------------------------------------------------
    def send(self, text):
        if not self.connected:
            self._sys("not connected — press O to open the selected port")
            return
        with self._lock:
            self._flush_partial()
        payload = (text + self.eol).encode("utf-8", "replace")
        try:
            os.write(self.fd, payload)
            self.tx_bytes += len(payload)
            self.lines.append(("» " + text if text else "» ⏎", "tx"))
            self.scroll = 0
        except OSError as e:
            self._sys(f"write failed: {e}")
            self.disconnect()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                break
            if not data:
                continue
            with self._lock:
                self.rx_bytes += len(data)
                self._ingest(data)

    def _ingest(self, data):
        if self.hexmode:
            self._hexbuf.extend(data)
            while len(self._hexbuf) >= 16:
                self.lines.append((self._hexline(self._hexbuf[:16]), "rx"))
                del self._hexbuf[:16]
            return
        for byte in data:
            if byte == 0x0A:
                self.lines.append((self._decode(self._partial), "rx"))
                self._partial = bytearray()
            elif byte == 0x0D:
                continue
            else:
                self._partial.append(byte)
                if len(self._partial) > 4096:
                    self.lines.append((self._decode(self._partial), "rx"))
                    self._partial = bytearray()

    def _flush_partial(self):
        if self._partial:
            self.lines.append((self._decode(self._partial), "rx"))
            self._partial = bytearray()
        if self._hexbuf:
            self.lines.append((self._hexline(self._hexbuf), "rx"))
            self._hexbuf = bytearray()

    @staticmethod
    def _decode(bs):
        return "".join(chr(b) if 32 <= b < 127 or b == 9 else "·" for b in bs)

    @staticmethod
    def _hexline(bs):
        hx = " ".join(f"{b:02X}" for b in bs)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in bs)
        return f"{hx:<47}  {asc}"

    def render_lines(self):
        """Scrollback plus the in-progress partial line, as (text, kind)."""
        with self._lock:
            out = list(self.lines)
            if self.hexmode and self._hexbuf:
                out.append((self._hexline(self._hexbuf), "rx"))
            elif self._partial:
                out.append((self._decode(self._partial), "rx"))
        return out

    @staticmethod
    def _explain(e, port):
        errno = getattr(e, "errno", None)
        if isinstance(e, PermissionError) or errno == 13:
            return f"{port}: permission denied — run  sudo usermod -aG dialout $USER  then log out and in"
        if errno == 2:
            return f"{port}: not found (unplugged?)"
        if errno == 16:
            return f"{port}: busy — another program has it open"
        return f"{port}: {e}"

    def close(self):
        self.disconnect(quiet=True)


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def init_colors():
    names = ("pri", "acc", "ok", "warn", "crit", "dim", "frame", "txt", "hdr", "hdr2",
             "alert", "mx", "mx2", "mxh", "ghost")
    C = {k: 0 for k in names}
    if not curses.has_colors():
        C.update(pri=BOLD, acc=BOLD, crit=BOLD, warn=BOLD, mxh=BOLD,
                 hdr=curses.A_REVERSE, hdr2=curses.A_REVERSE | BOLD, alert=curses.A_REVERSE)
        return C, [BOLD], False
    curses.start_color()
    rich = curses.COLORS >= 256
    k, bk = curses, curses.COLOR_BLACK

    def c(r, b):
        return r if rich else b
    spec = [
        ("pri", c(51, k.COLOR_CYAN), bk),
        ("acc", c(201, k.COLOR_MAGENTA), bk),
        ("ok", c(48, k.COLOR_GREEN), bk),
        ("warn", c(214, k.COLOR_YELLOW), bk),
        ("crit", c(196, k.COLOR_RED), bk),
        ("dim", c(243, k.COLOR_WHITE), bk),
        ("frame", c(30, k.COLOR_CYAN), bk),
        ("txt", c(253, k.COLOR_WHITE), bk),
        ("hdr", bk, c(51, k.COLOR_CYAN)),
        ("hdr2", bk, c(201, k.COLOR_MAGENTA)),
        ("alert", c(231, k.COLOR_WHITE), c(160, k.COLOR_RED)),
        ("mx", c(46, k.COLOR_GREEN), bk),
        ("mx2", c(22, k.COLOR_GREEN), bk),
        ("mxh", c(231, k.COLOR_WHITE), bk),
        ("ghost", c(236, bk), bk),
    ]
    for i, (name, fg, bg) in enumerate(spec, start=1):
        curses.init_pair(i, fg, bg)
        C[name] = curses.color_pair(i)
    if not rich:
        for name in ("pri", "acc", "ok", "warn", "crit", "txt", "mxh", "mx"):
            C[name] |= BOLD
        C["ghost"] = 0          # can't do a dark grey with 8 colours
    shades = [51, 45, 39, 33, 69, 99, 135, 171, 207, 201] if rich else \
        [k.COLOR_CYAN, k.COLOR_BLUE, k.COLOR_MAGENTA]
    grad = []
    base = len(spec) + 1
    for j, col in enumerate(shades):
        curses.init_pair(base + j, col, bk)
        grad.append(curses.color_pair(base + j) | (0 if rich else BOLD))
    return C, grad, rich


class Painter:
    def __init__(self, scr, C):
        self.s = scr
        self.C = C
        self.h, self.w = scr.getmaxyx()

    def sync(self):
        self.h, self.w = self.s.getmaxyx()

    def put(self, y, x, text, attr=0):
        if y < 0 or y >= self.h or x >= self.w or not text:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: self.w - x]
        if not text:
            return
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def ink(self, y, x, s, attr):
        """put() that leaves spaces transparent"""
        i, n = 0, len(s)
        while i < n:
            if s[i] == " ":
                i += 1
                continue
            j = i
            while j < n and s[j] != " ":
                j += 1
            self.put(y, x + i, s[i:j], attr)
            i = j

    def fill(self, y, x, h, w, ch=" ", attr=0):
        for r in range(h):
            self.put(y + r, x, ch * w, attr)

    def box(self, y, x, h, w, title=None, tag=None, tag_attr=None, attr=None):
        if h < 2 or w < 6:
            return
        a = self.C["frame"] if attr is None else attr
        hi = self.C["pri"] if attr is None else attr
        self.put(y, x, "┏" + "━" * (w - 2) + "┓", a)
        for r in range(1, h - 1):
            self.put(y + r, x, "┃", a)
            self.put(y + r, x + w - 1, "┃", a)
        self.put(y + h - 1, x, "┗" + "━" * (w - 2) + "┛", a)
        # HUD-style bright corners
        self.put(y, x, "┏━", hi)
        self.put(y, x + w - 2, "━┓", hi)
        self.put(y + h - 1, x, "┗━", hi)
        self.put(y + h - 1, x + w - 2, "━┛", hi)
        tlen = 0
        if title:
            t = clip(title, w - 8)
            tlen = len(t)
            self.put(y, x + 2, "┫", a)
            self.put(y, x + 3, f" {t} ", self.C["pri"] | BOLD)
            self.put(y, x + 5 + tlen, "┣", a)
        if tag:
            tg = clip(tag, w - tlen - 14)
            if tg:
                tx = x + w - len(tg) - 4
                self.put(y, tx - 1, "┫", a)
                self.put(y, tx, f" {tg} ", self.C["dim"] if tag_attr is None else tag_attr)
                self.put(y, tx + len(tg) + 2, "┣", a)

    def level(self, frac, warn=0.6, crit=0.85):
        return self.C["crit"] if frac >= crit else self.C["warn"] if frac >= warn else self.C["ok"]

    def bar(self, y, x, w, frac, attr=None, empty="░"):
        if w <= 0:
            return
        frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
        cells = frac * w
        full = int(cells)
        s = "█" * full
        idx = int((cells - full) * 8)
        if full < w and idx > 0:
            s += " ▏▎▍▌▋▊▉"[idx]
        self.put(y, x, s, self.level(frac) if attr is None else attr)
        if len(s) < w:
            self.put(y, x + len(s), empty * (w - len(s)), self.C["dim"])

    def spark(self, y, x, w, values, vmax=None, vmin=0.0, attr=None):
        if w <= 0:
            return
        vals = list(values)[-w:]
        if vals:
            hi = vmax if vmax is not None else max(vals)
            if hi <= vmin:
                hi = vmin + 1
            s = "".join(SPARK[max(0, min(7, int((v - vmin) / (hi - vmin) * 7.999)))] for v in vals)
        else:
            s = ""
        pad = w - len(s)
        if pad:
            self.put(y, x, "▁" * pad, self.C["ghost"] or self.C["dim"])
        self.put(y, x + pad, s, self.C["pri"] if attr is None else attr)

    def graph(self, y, x, h, w, values, vmax, attrs):
        if h <= 0 or w <= 0:
            return
        grid = self.C["ghost"] or self.C["dim"]
        for frac in (0.25, 0.5, 0.75):
            gy = y + h - 1 - int(frac * h)
            if y <= gy < y + h:
                self.put(gy, x, ("·   " * (w // 4 + 1))[:w], grid)
        vals = list(values)[-w:]
        pad = w - len(vals)
        bl = " ▁▂▃▄▅▆▇█"
        multi = isinstance(attrs, list)
        for i, v in enumerate(vals):
            f = 0.0 if vmax <= 0 else max(0.0, min(1.0, v / vmax))
            units = f * h * 8
            if v > 0 and units < 1:
                units = 1
            cx = x + pad + i
            for r in range(h):
                cell = units - r * 8
                if cell < 1:
                    break
                ch = "█" if cell >= 8 else bl[int(cell)]
                a = attrs[min(len(attrs) - 1, r * len(attrs) // h)] if multi else attrs
                self.put(y + h - 1 - r, cx, ch, a)


class SkipBoot(Exception):
    pass


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------

class App:
    def __init__(self, scr, args):
        self.scr = scr
        self.args = args
        self.C, self.grad, self.rich = init_colors()
        try:
            scr.bkgd(" ", self.C["txt"])
        except curses.error:
            pass
        self.P = Painter(scr, self.C)
        self.log = EventLog()
        self.sys = SysMon()
        self.tach = Tach(args.pin, args.ppr, args.sim, args.debounce)
        self.procs = ProcMon()
        self.diag = NetDiag(self.sys, self.log, args.ping_host)
        self.console = Console()
        self.sw = Stopwatch()
        self.timer = Timer(args.timer)
        self.notes = Notes(NOTES_FILE)
        self.keys = KeyGen()
        self.recon = Recon(self.log)
        self.wireless = Wireless(self.log)
        self.kit = Kit(self.log)
        self.serial = SerialLink(self.log, args.baud)
        self.pentest_mode = 0
        self.pt_hits = []
        self._kit_arm = False

        self.page = 0
        self.running = True
        self.frame = 0
        self.overlay = None
        self.saver = False
        self.matrix = None
        self.input_mode = None
        self.input_buf = ""
        self.cmd_hist = []
        self.hist_idx = None
        self.alarm = False
        self.alerts = []
        self._alert_keys = set()
        self._temp_warned = False
        self.tab_hits = []
        self.proc_sort = "cpu"
        self.rotor = 0.0
        self.last_input = time.monotonic()
        self._next_sys = 0.0
        self._next_proc = 0.0
        self.ticker_phrase = random.choice(PHRASES)
        self.ticker_pos = 0
        self.post_results = None

        self.procs.sample(self.sys.mem.get("MemTotal", 0))
        self.log.add(f"CYBERDECK v{VERSION} online: {self.sys.model}", "ok")
        T = self.tach
        if T.backend == "sim":
            self.log.add("tach: SIMULATION mode (--sim), not reading hardware", "warn")
        elif T.online:
            self.log.add(f"tach: GPIO{T.pin} via {T.backend} {T.detail}, {T.ppr} pulses/rev", "ok")
        else:
            self.log.add(f"tach: GPIO{T.pin} unavailable: {T.error}", "crit")

    # ---- shared bits ------------------------------------------------------
    def tach_status(self):
        T, C = self.tach, self.C
        if T.backend == "sim":
            return "SIMULATED", C["warn"] | BOLD
        if not T.online:
            return "OFFLINE", C["crit"] | BOLD
        if T.stalled:
            return "STALL", C["crit"] | BOLD
        return "LIVE", C["ok"] | BOLD

    def gauge_max(self):
        if self.args.max_rpm > 0:
            return self.args.max_rpm
        peak = max(self.tach.rpm_max or 0.0, self.tach.rpm)
        return max(1000, int(math.ceil(peak * 1.15 / 500.0)) * 500)

    def power_status(self):
        S, C = self.sys, self.C
        t = S.throttled
        if t is None:
            return "n/a (no vcgencmd)", C["dim"]
        v = f"  {S.volts}" if S.volts else ""
        now = [n for b, n in THROTTLE_NOW if t & (1 << b)]
        past = [n for b, n in THROTTLE_PAST if t & (1 << b)]
        if now:
            return "!! " + ", ".join(now) + v, C["crit"] | BOLD
        if past:
            return "OK, earlier: " + ", ".join(past) + v, C["warn"]
        return "STABLE" + v, C["ok"]

    def visible_ifaces(self):
        S = self.sys
        names = [n for n in S.net if n in S.ips or S.net[n]["rx"] > 0]

        def prio(n):
            return (0 if n in S.ips else 1, 0 if n.startswith(("wlan", "eth", "en", "wl")) else 1, n)
        return sorted(names, key=prio)

    def draw_big(self, y, x, text, attr, sx=1, sy=1, ghost=True):
        """Draw scalable 7-segment style digits with unlit '8' ghosts behind them."""
        gattr = self.C.get("ghost")
        if ghost and gattr:
            gtext = "".join("8" if (c.isdigit() or c == "_") else c for c in text)
            for r, row in enumerate(pix_render(gtext, DIGITS, sx, sy)):
                self.P.ink(y + r, x, row, gattr)
        rows = pix_render(text, DIGITS, sx, sy)
        for r, row in enumerate(rows):
            self.P.ink(y + r, x, row, attr)
        return len(rows[0]) if rows else 0

    def vstack(self, y, x, w, h, items):
        """Stack panels vertically. items: (min, want, priority, grows, draw_fn)."""
        for it, hh in zip(items, alloc(h, [it[:4] for it in items])):
            if hh:
                it[4](y, x, hh, w)
                y += hh

    def post_checks(self):
        S, T = self.sys, self.tach
        h, w = self.P.h, self.P.w
        out = []
        fq = f" @ {S.max_freq} MHz" if S.max_freq else ""
        out.append(("CPU", f"{S.cpu_name} x{S.ncpu}{fq}", "OK"))
        mt = S.mem.get("MemTotal", 0) // 1024
        out.append(("MEMORY", f"{mt} MB", "OK" if mt else "WARN"))
        if S.disk:
            pct = S.disk.used / S.disk.total if S.disk.total else 0
            out.append(("STORAGE", f"{fmt_bytes(S.disk.used)} / {fmt_bytes(S.disk.total)} ({pct * 100:.0f}%)",
                        "WARN" if pct > 0.9 else "OK"))
        if S.temp is not None:
            out.append(("THERMAL", f"{S.temp:.1f}°C", "WARN" if S.temp >= self.args.temp_warn else "OK"))
        else:
            out.append(("THERMAL", "no sensor", "WARN"))
        pw, _ = self.power_status()
        out.append(("POWER", pw, "WARN" if (S.throttled is None or S.throttled & 0xF) else "OK"))
        ifs = self.visible_ifaces()
        ip_if = next((n for n in ifs if n in S.ips), None) or next(iter(S.ips), None)
        out.append(("UPLINK", f"{ip_if} {S.ips[ip_if]}" if ip_if else "NO CARRIER", "OK" if ip_if else "WARN"))
        if T.backend == "sim":
            out.append((f"TACH GPIO{T.pin}", "simulated signal", "SIM"))
        elif T.online:
            out.append((f"TACH GPIO{T.pin}", f"{T.backend} {T.detail}  {T.ppr} pulses/rev", "OK"))
        else:
            out.append((f"TACH GPIO{T.pin}", T.error or "unavailable", "FAIL"))
        out.append(("DISPLAY", f"{w}x{h}  {curses.COLORS if curses.has_colors() else 2} colours", "OK"))
        return out

    # ---- per-frame logic --------------------------------------------------
    def eval_alerts(self):
        S, T, a = self.sys, self.tach, []
        if T.online and T.stalled:
            a.append(("fan", f"FAN STALL: NO TACH PULSES ON GPIO{T.pin}"))
        if S.temp is not None and S.temp >= self.args.temp_crit:
            a.append(("temp", f"CPU TEMPERATURE CRITICAL {S.temp:.1f}°C"))
        if S.throttled is not None and S.throttled & 0x1:
            a.append(("volt", "UNDER-VOLTAGE DETECTED: CHECK PSU"))
        keys = {k for k, _ in a}
        for k, msg in a:
            if k not in self._alert_keys:
                self.log.add(msg, "crit")
        cleared = {"fan": "fan spinning again", "temp": "temperature back in range",
                   "volt": "supply voltage recovered"}
        for k in self._alert_keys - keys:
            self.log.add(cleared[k], "ok")
        self._alert_keys = keys
        self.alerts = a
        if S.temp is not None:
            if S.temp >= self.args.temp_warn and not self._temp_warned:
                self.log.add(f"CPU temperature high: {S.temp:.1f}°C", "warn")
                self._temp_warned = True
            elif S.temp < self.args.temp_warn - 3:
                self._temp_warned = False

    def tick(self):
        now = time.monotonic()
        self.tach.update()
        if now >= self._next_sys:
            self._next_sys = now + 1.0
            self.sys.sample()
            self.eval_alerts()
        if PAGES[self.page] in ("DASH", "PROC") and now >= self._next_proc:
            self._next_proc = now + 2.0
            self.procs.sample(self.sys.mem.get("MemTotal", 0))
        if self.timer.check():
            self.alarm = True
            self.log.add("countdown timer complete", "warn")
        if self.alarm and self.frame % 10 == 0:
            curses.beep()
        if self.args.saver > 0 and not self.saver and now - self.last_input > self.args.saver * 60:
            self.saver = True
        self.rotor += min(self.tach.rpm, 6000.0) / 6000.0 * 0.9

    # ---- main loop ----------------------------------------------------------
    def run(self):
        s = self.scr
        try:
            while self.running:
                self.frame += 1
                self.tick()
                self.draw()
                s.timeout(60 if self.saver else 100)
                try:
                    k = s.get_wch()
                except curses.error:
                    k = None
                if k is not None:
                    self.handle_key(k)
        finally:
            self.tach.close()
            self.serial.close()

    # ---- input --------------------------------------------------------------
    def handle_key(self, k):
        self.last_input = time.monotonic()
        if k == curses.KEY_RESIZE:
            return
        if self.saver:
            self.saver = False
            return
        if self.alarm:
            self.alarm = False
            return
        if self.input_mode:
            self.handle_input(k)
            return
        if k == curses.KEY_MOUSE:
            self.handle_mouse()
            return
        if self.overlay == "power":
            low = k.lower() if isinstance(k, str) else ""
            self.overlay = None
            if low == "r":
                self.power("reboot")
            elif low == "s":
                self.power("poweroff")
            return
        if self.overlay:
            self.overlay = None
            return
        ch = k if isinstance(k, str) else ""
        low = ch.lower()
        if ch and ch.isdigit() and 1 <= int(ch) <= len(PAGES):
            self.page = int(ch) - 1
        elif ch == "\t" or k == curses.KEY_RIGHT:
            self.page = (self.page + 1) % len(PAGES)
        elif k in (curses.KEY_BTAB, curses.KEY_LEFT):
            self.page = (self.page - 1) % len(PAGES)
        elif low == "q":
            self.running = False
        elif low == "m":
            self.saver = True
        elif ch == "?" or low == "h":
            self.overlay = "help"
        elif ch in ("!", ":"):
            self.page = PAGES.index("TERM")
            self.begin_input("cmd")
        elif low == "p":
            self.overlay = "power"
        else:
            self.page_key(k, ch, low)

    def page_key(self, k, ch, low):
        name = PAGES[self.page]
        if name == "FAN" and low == "r":
            self.tach.reset_stats()
            self.log.add("tach stats reset", "info")
        elif name == "NET" and low == "d":
            self.diag.start()
        elif name == "PROC" and low == "s":
            self.proc_sort = "mem" if self.proc_sort == "cpu" else "cpu"
        elif name == "PTEST":
            self.pentest_key(k, ch, low)
        elif name == "SERIAL":
            self.serial_key(k, ch, low)
        elif name == "TOOLS":
            if ch == " ":
                self.sw.toggle()
            elif low == "l":
                self.sw.lap()
            elif low == "z":
                self.sw.reset()
            elif low == "t":
                self.timer.toggle()
                if self.timer.running:
                    self.log.add(f"countdown started: {fmt_dur(self.timer.left())}", "info")
            elif ch in ("+", "="):
                self.timer.adjust(60)
            elif ch in ("-", "_"):
                self.timer.adjust(-60)
            elif low == "r":
                self.timer.reset()
            elif low == "n":
                self.begin_input("note")
            elif low == "g":
                self.keys.gen()
        elif name == "TERM":
            con = self.console
            if low == "k":
                con.kill()
            elif low == "c":
                con.clear()
            elif k == curses.KEY_UP:
                con.scroll = min(con.max_scroll, con.scroll + 1)
            elif k == curses.KEY_DOWN:
                con.scroll = max(0, con.scroll - 1)
            elif k == curses.KEY_PPAGE:
                con.scroll = min(con.max_scroll, con.scroll + 10)
            elif k == curses.KEY_NPAGE:
                con.scroll = max(0, con.scroll - 10)
            elif k == curses.KEY_END:
                con.scroll = 0

    def pentest_key(self, k, ch, low):
        if low == "i" and self.PT_SECTIONS[self.pentest_mode] == "KIT":
            self._kit_install()
            return
        self._kit_arm = False               # any other key cancels an armed install
        if ch == "[":
            self.pentest_mode = (self.pentest_mode - 1) % len(self.PT_SECTIONS)
        elif ch == "]":
            self.pentest_mode = (self.pentest_mode + 1) % len(self.PT_SECTIONS)
        elif low == "s":
            self._pt_scan(False)
        elif low == "f":
            self._pt_scan(True)

    def _pt_scan(self, ports):
        mode = self.PT_SECTIONS[self.pentest_mode]
        if mode == "RECON":
            self.recon.start(with_ports=ports)
        elif mode == "WIRELESS":
            self.wireless.start()
        elif mode == "KIT":
            self.kit.start()

    def _kit_install(self):
        if self.kit.installing or not self.kit.rows:
            return
        if not self.kit.missing_packages():
            self._kit_arm = False
            self.kit.install_log.append(("all tools already installed", "ok"))
            return
        if not self._kit_arm:          # first press arms, second press confirms
            self._kit_arm = True
            return
        self._kit_arm = False
        self.kit.install_all()

    def serial_key(self, k, ch, low):
        L = self.serial
        if k in ("\n", "\r") or k == curses.KEY_ENTER:
            if L.connected:
                self.begin_input("serial")
            else:
                L.connect()
        elif ch == "[":
            L.select(-1)
        elif ch == "]":
            L.select(1)
        elif low == "o":
            L.connect()
        elif low == "d":
            L.disconnect()
        elif low == "r":
            L.refresh_ports(force=True)
        elif low == "b":
            L.cycle_baud(1)
        elif low == "e":
            L.cycle_eol()
        elif low == "x":
            L.toggle_hex()
        elif low == "w":
            L.clear()
        elif k == curses.KEY_UP:
            L.scroll = min(L.max_scroll, L.scroll + 1)
        elif k == curses.KEY_DOWN:
            L.scroll = max(0, L.scroll - 1)
        elif k == curses.KEY_PPAGE:
            L.scroll = min(L.max_scroll, L.scroll + 10)
        elif k == curses.KEY_NPAGE:
            L.scroll = max(0, L.scroll - 10)
        elif k == curses.KEY_END:
            L.scroll = 0

    def begin_input(self, mode):
        self.input_mode = mode
        self.input_buf = ""
        self.hist_idx = None

    def handle_input(self, k):
        if k in ("\n", "\r") or k == curses.KEY_ENTER:
            if self.input_mode == "serial":
                self.serial.send(self.input_buf)
                self.input_buf = ""
                return
            text, mode = self.input_buf.strip(), self.input_mode
            self.input_mode, self.input_buf = None, ""
            if not text:
                return
            if mode == "cmd":
                self.cmd_hist.append(text)
                self.page = PAGES.index("TERM")
                if text in ("clear", "cls"):
                    self.console.clear()
                else:
                    self.console.run(text)
                    self.log.add(f"exec: {clip(text, 60)}", "sys")
            elif mode == "note":
                err = self.notes.add(text)
                self.log.add("note saved" if not err else f"note not saved: {err}", "ok" if not err else "crit")
        elif k == "\x1b":
            self.input_mode, self.input_buf = None, ""
        elif k in ("\x7f", "\b") or k == curses.KEY_BACKSPACE:
            self.input_buf = self.input_buf[:-1]
        elif k == "\x15":
            self.input_buf = ""
        elif k == curses.KEY_UP and self.input_mode == "cmd" and self.cmd_hist:
            self.hist_idx = len(self.cmd_hist) - 1 if self.hist_idx is None else max(0, self.hist_idx - 1)
            self.input_buf = self.cmd_hist[self.hist_idx]
        elif k == curses.KEY_DOWN and self.input_mode == "cmd" and self.hist_idx is not None:
            self.hist_idx += 1
            if self.hist_idx >= len(self.cmd_hist):
                self.hist_idx, self.input_buf = None, ""
            else:
                self.input_buf = self.cmd_hist[self.hist_idx]
        elif isinstance(k, str) and k.isprintable():
            self.input_buf += k

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if self.overlay:
            self.overlay = None
            return
        b5 = getattr(curses, "BUTTON5_PRESSED", 0x200000)
        if PAGES[self.page] == "TERM" and bstate & curses.BUTTON4_PRESSED:
            self.console.scroll = min(self.console.max_scroll, self.console.scroll + 3)
            return
        if PAGES[self.page] == "TERM" and bstate & b5:
            self.console.scroll = max(0, self.console.scroll - 3)
            return
        if PAGES[self.page] == "SERIAL" and bstate & curses.BUTTON4_PRESSED:
            self.serial.scroll = min(self.serial.max_scroll, self.serial.scroll + 3)
            return
        if PAGES[self.page] == "SERIAL" and bstate & b5:
            self.serial.scroll = max(0, self.serial.scroll - 3)
            return
        if PAGES[self.page] == "PTEST" and my == getattr(self, "pt_row", -1):
            for x0, x1, i in self.pt_hits:
                if x0 <= mx < x1:
                    self.pentest_mode = i
            return
        if my == 0:
            for x0, x1, i in self.tab_hits:
                if x0 <= mx < x1:
                    self.page = i

    def power(self, action):
        self.log.add(f"power: {action} requested", "warn")
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", action], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                self.log.add(f"power {action} failed: {(r.stderr or r.stdout).strip()[:80]}", "crit")
        except (OSError, subprocess.SubprocessError) as e:
            self.log.add(f"power {action} failed: {e}", "crit")

    # ---- frame --------------------------------------------------------------
    def draw(self):
        s, P = self.scr, self.P
        P.sync()
        h, w = P.h, P.w
        s.erase()
        if self.saver:
            self.draw_saver(h, w)
        elif h < MIN_H or w < MIN_W:
            for i, (msg, a) in enumerate(((f"need {MIN_W}x{MIN_H}", self.C["warn"] | BOLD),
                                          (f"now {w}x{h}", self.C["txt"]),
                                          ("enlarge or Q", self.C["dim"]))):
                P.put(h // 2 - 1 + i, max(0, (w - len(msg)) // 2), msg, a)
        else:
            bars = 2 if h >= 16 else 1          # header / footer rows; slim on short screens
            self.draw_header(w, bars)
            pages = [self.page_dash, self.page_fan, self.page_net,
                     self.page_proc, self.page_tools, self.page_term,
                     self.page_pentest, self.page_serial]
            pages[self.page](bars, h - 2 * bars, w)
            self.draw_footer(h, w, bars)
            if self.overlay == "help":
                self.draw_help()
            elif self.overlay == "power":
                self.draw_power()
            if self.alarm:
                self.draw_alarm()
        s.refresh()

    def tab_label(self, i, compact):
        if compact and i != self.page:
            return f" {i + 1} "
        return f" {i + 1}:{PAGES[i]} "

    def draw_header(self, w, rows=2):
        P, C, S = self.P, self.C, self.sys
        # pick the roomiest header that fits: full logo + full tabs, then shrink
        logo, compact = "", True
        for lg in (" ▓▒░ CYBERDECK ░▒▓ ", " CYBERDECK ", " ◢◤ "):
            for cp in (False, True):
                need = len(lg) + 1 + sum(len(self.tab_label(i, cp)) for i in range(len(PAGES)))
                if need <= w:
                    logo, compact = lg, cp
                    break
            if logo:
                break
        if len(logo) > 12 and random.random() < 0.04:
            i = random.randrange(5, len(logo) - 5)
            logo = logo[:i] + random.choice("▓▒░#%&/") + logo[i + 1:]
        P.put(0, 0, logo, C["hdr"])
        x = len(logo) + 1 if logo else 0
        self.tab_hits = []
        for i in range(len(PAGES)):
            label = self.tab_label(i, compact)
            P.put(0, x, label, C["hdr2"] if i == self.page else C["pri"])
            self.tab_hits.append((x, x + len(label), i))
            x += len(label)
        clock = time.strftime("%H:%M:%S")
        for r in (f" {S.hostname} ◆ {time.strftime('%a %d %b')} {clock} ", f" {S.hostname} ◆ {clock} ",
                  f" {clock} ", f" {clock[:5]} "):
            if x + len(r) + 1 <= w:
                P.put(0, w - len(r), r, C["acc"] | BOLD)
                break
        if rows < 2:
            return
        # rule, then the sweeping scanner, then the active-tab underline ON TOP
        # so the scanner slides behind the selected tab instead of clipping it
        P.put(1, 0, "─" * w, C["frame"])
        span = max(1, w - 10)
        pos = self.frame % (2 * span)
        pos = pos if pos < span else 2 * span - pos
        active = next(((x0, x1) for x0, x1, i in self.tab_hits if i == self.page), None)
        for c in range(pos, min(pos + 10, w)):
            if active and active[0] <= c < active[1]:
                continue                       # leave the selected tab's cells for the underline
            P.put(1, c, "━", C["pri"] | BOLD)
        if active:
            P.put(1, active[0], "▀" * (active[1] - active[0]), C["acc"])

    def build_ticker(self, w):
        """Live status feed. Numbers are fixed-width so the scroll never jumps."""
        S, T = self.sys, self.tach
        parts = ["▶ CYBERDECK STATUS FEED", f"CPU {S.cpu_total * 100:3.0f}%"]
        if S.temp is not None:
            parts.append(f"CORE {S.temp:5.1f}°C")
        parts.append(f"FAN {T.rpm:5.0f} RPM" if T.online else "FAN TACH OFFLINE")
        mt = S.mem.get("MemTotal", 0)
        if mt:
            parts.append(f"MEM {100 - S.mem.get('MemAvailable', 0) / mt * 100:3.0f}%")
        if S.ips:
            n = next(iter(S.ips))
            parts.append(f"{n.upper()} {S.ips[n]}")
        parts.append(self.ticker_phrase)
        text = "   ◆   ".join(parts) + "   ◆   "
        while len(text) < w:
            text += text
        return text

    def draw_footer(self, h, w, rows=2):
        P, C = self.P, self.C
        if self.alerts and not self.input_mode:
            msg = "   ▲   ".join(m for _, m in self.alerts)
            flash = (self.frame // 4) % 2 == 0
            P.put(h - rows, 0, f" ▲ {msg} ▲ ".center(w)[:w], (C["alert"] if flash else C["crit"]) | BOLD)
            if rows < 2:
                return            # on short screens the alert takes the only footer row
        elif rows < 2:
            pass
        else:
            text = self.build_ticker(w)
            if self.ticker_pos >= len(text):
                self.ticker_pos = 0
                self.ticker_phrase = random.choice(PHRASES)
                text = self.build_ticker(w)
            src = text + text
            P.put(h - 2, 0, src[self.ticker_pos:self.ticker_pos + w], C["dim"])
            if self.frame % 2 == 0:
                self.ticker_pos += 1

        y = h - 1
        if self.input_mode:
            label = {"cmd": " CMD ▸ ", "note": " NOTE ▸ ", "serial": " TX ▸ "}.get(self.input_mode, " ▸ ")
            send = self.input_mode == "serial"
            right = (" ENTER send · ESC done " if send else " ENTER ok · ESC cancel ") if w >= 60 else ""
            P.put(y, 0, label, C["hdr2"])
            room = w - len(label) - len(right) - 3
            buf = self.input_buf[-room:] if room > 0 else ""
            P.put(y, len(label) + 1, buf, C["txt"] | BOLD)
            if self.frame % 6 < 3:
                P.put(y, len(label) + 1 + len(buf), "█", C["pri"])
            P.put(y, w - len(right) - 1, right, C["dim"])
            return
        page_hints = {
            "FAN": [("R", "RESET STATS")],
            "NET": [("D", "DIAGNOSE")],
            "PROC": [("S", "SORT CPU/MEM")],
            "TOOLS": [("SPC", "CHRONO"), ("L", "LAP"), ("Z", "ZERO"), ("T", "TIMER"),
                      ("+/-", "MIN"), ("R", "RESET"), ("N", "NOTE"), ("G", "KEYGEN")],
            "TERM": [("!", "RUN"), ("K", "KILL"), ("C", "CLEAR"), ("↑↓", "SCROLL")],
            "PTEST": [("[ ]", "SECTION"), ("S", "SCAN"), ("F", "+PORTS"), ("I", "INSTALL")],
            "SERIAL": [("[ ]", "PORT"), ("O", "OPEN"), ("D", "CLOSE"), ("ENTER", "SEND"),
                       ("B", "BAUD"), ("E", "EOL"), ("X", "HEX"), ("W", "WIPE")],
        }.get(PAGES[self.page], [])
        hints = page_hints + [("1-8", "PAGE"), ("!", "CMD"), ("M", "MATRIX"), ("P", "POWER")]
        tail = [("?", "HELP"), ("Q", "QUIT")]

        def width(items):
            return sum(len(k) + len(lbl) + 3 for k, lbl in items)
        x = 0
        limit = w - width(tail) - 1
        seen = set()
        for key, lbl in hints:
            if key in seen:
                continue
            seen.add(key)
            need = len(key) + len(lbl) + 3
            if x + need > limit:
                break
            P.put(y, x, f" {key} ", C["hdr"])
            P.put(y, x + len(key) + 2, f"{lbl} ", C["dim"])
            x += need
        x = w - width(tail) - 1
        for key, lbl in tail:
            P.put(y, x, f" {key} ", C["hdr"])
            P.put(y, x + len(key) + 2, f"{lbl} ", C["dim"])
            x += len(key) + len(lbl) + 3

    # ---- pages ---------------------------------------------------------------
    def page_dash(self, y0, h, w):
        S = self.sys
        lw = w // 2
        nif = max(1, min(len(self.visible_ifaces()), 4))
        # (min rows, wanted rows, priority, grows, draw)
        cpu = (3, S.ncpu + 4, 1, False, self.box_cpu)
        mem = (3, 5, 3, False, self.box_mem)
        sysb = (3, 8, 5, True, self.box_sys)
        fan = (3, 7 if h < 34 else 10 if h < 50 else 13, 0, False, self.box_fan_mini)
        therm = (3, 4, 2, False, self.box_thermal)
        net = (3, nif + 3, 4, False, self.box_net_mini)
        top = (4, 40, 6, True, self.box_top)
        if w >= 80:
            self.vstack(y0, 0, lw, h, [cpu, mem, sysb])
            self.vstack(y0, lw, w - lw, h, [fan, therm, net, top])
        else:
            self.vstack(y0, 0, w, h, [fan, cpu, therm, mem, net, sysb, top])

    def box_cpu(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        fq = S.freqs[0] if S.freqs else None
        P.box(y, x, h, w, f"CPU // {S.ncpu} CORES", tag=f"{fq} MHz" if fq else None)
        bx, iw = x + 2, w - 4
        bw = iw - 11
        inner = h - 2
        ncore = S.ncpu if inner >= S.ncpu + 1 else 0      # all cores or none, never a partial list
        yy = y + 1
        for i in range(ncore):
            u = S.cpu_cores[i] if i < len(S.cpu_cores) else 0.0
            P.put(yy, bx, f"CORE{i:<2}"[:6], C["dim"])
            P.bar(yy, bx + 6, bw, u)
            P.put(yy, bx + 6 + bw, f"{u * 100:4.0f}%", C["txt"])
            yy += 1
        P.put(yy, bx, "TOTAL", C["pri"] | BOLD)
        P.bar(yy, bx + 6, bw, S.cpu_total)
        P.put(yy, bx + 6 + bw, f"{S.cpu_total * 100:4.0f}%", C["pri"] | BOLD)
        if yy + 1 < y + h - 1:
            P.put(yy + 1, bx, "HIST", C["dim"])
            P.spark(yy + 1, bx + 6, iw - 6, [v * 100 for v in S.cpu_hist], vmax=100)

    def box_mem(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        P.box(y, x, h, w, "MEMORY // STORAGE")
        m = S.mem
        tot = m.get("MemTotal", 0) * 1024
        used = tot - m.get("MemAvailable", 0) * 1024
        st = m.get("SwapTotal", 0) * 1024
        su = st - m.get("SwapFree", 0) * 1024
        rows = [("RAM", used, tot), ("SWAP", su, st)]
        if S.disk:
            rows.append(("DISK", S.disk.used, S.disk.total))
        iw, vw = w - 4, 12
        bw = iw - 5 - vw - 1
        for i, (lab, u, t) in enumerate(rows[:h - 2]):
            P.put(y + 1 + i, x + 2, lab, C["dim"])
            P.bar(y + 1 + i, x + 7, bw, u / t if t else 0)
            P.put(y + 1 + i, x + 7 + bw + 1, f"{fmt_bytes(u)}/{fmt_bytes(t)}".rjust(vw), C["txt"])

    def box_sys(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        P.box(y, x, h, w, "SYSTEM", tag=f"{S.user}@{S.hostname}")
        pw, pattr = self.power_status()
        load = "  ".join(f"{v:.2f}" for v in S.load)
        mt = S.mem.get("MemTotal", 0)
        ip_if = next((n for n in self.visible_ifaces() if n in S.ips), None) or next(iter(S.ips), None)
        booted = datetime.datetime.now() - datetime.timedelta(seconds=S.uptime)
        fq = f"  max {S.max_freq} MHz" if S.max_freq else ""
        lines = [  # (priority, label, value, attr)
            (0, "MODEL", S.model, C["txt"]),
            (6, "CPU", f"{S.cpu_name} x{S.ncpu}{fq}", C["txt"]),
            (4, "OS", S.os_name, C["txt"]),
            (5, "KERNEL", f"{S.kernel} {S.arch}", C["txt"]),
            (7, "ADDR", f"{S.ips[ip_if]} ({ip_if})" if ip_if else "offline", C["acc"] if ip_if else C["dim"]),
            (1, "UPTIME", fmt_dur(S.uptime), C["txt"]),
            (8, "BOOTED", f"{booted:%a %d %b %H:%M}", C["txt"]),
            (3, "LOAD", load, C["txt"]),
            (9, "TASKS", f"{self.procs.total} procs · {self.procs.threads} threads", C["txt"]),
            (10, "RAM", f"{mt // 1024} MB total", C["txt"]),
            (2, "POWER", pw, pattr),
        ]
        keep = sorted(lines, key=lambda r: r[0])[:max(0, h - 2)]
        shown = [r for r in lines if r in keep]
        for i, (_, lab, val, a) in enumerate(shown):
            P.put(y + 1 + i, x + 2, f"{lab:<7}", C["dim"])
            P.put(y + 1 + i, x + 9, clip(val, w - 11), a)

    def box_fan_mini(self, y, x, h, w):
        P, C, T = self.P, self.C, self.tach
        st, sa = self.tach_status()
        P.box(y, x, h, w, f"FAN // GPIO{T.pin}", tag=st, tag_attr=sa)
        bx, iw, inner = x + 2, w - 4, h - 2
        if not T.online:
            lines = [("NO SIGNAL", C["crit"] | BOLD)]
            lines += [(seg, C["dim"]) for seg in wrap(T.error or "tach not initialised", iw)[:2]]
            lines.append((clip("wiring help on the FAN page [2]", iw), C["warn"]))
            for i, (text, a) in enumerate(lines[:inner]):
                P.put(y + 1 + i, bx, text, a)
            return
        rpm = int(round(T.rpm))
        txt = (f"{rpm:>4d}" if rpm < 10000 else str(rpm)).replace(" ", "_")
        attr = C["crit"] | BOLD if T.stalled else C["pri"] | BOLD
        status = ("STALLED", C["crit"] | BOLD) if T.stalled else ("SPINNING", C["ok"])
        foot = 2 if inner >= 5 else 1 if inner >= 4 else 0       # gauge + sparkline rows
        dig_h = inner - foot
        fit = pix_fit(txt, DIGITS, iw - 12, dig_h, MINI_LADDER) if dig_h >= 3 else None
        if fit:
            bw = self.draw_big(y + 1, bx, txt, attr, *fit)
            dh = pix_size(txt, DIGITS, *fit)[1]
            lx = bx + bw + 2
            ly = y + 1 + max(0, (dh - 3) // 2)
            P.put(ly, lx, "RPM", C["acc"] | BOLD)
            P.put(ly + 1, lx, f"{T.hz:.1f} Hz", C["txt"])
            P.put(ly + 2, lx, status[0], status[1])
            rx = x + w - 8
            if rx > lx + 10:
                for r, s in enumerate(ROTOR[int(self.rotor) % 2]):
                    P.ink(ly + r, rx, s, C["acc"] if T.rpm > 0 else C["dim"])
        else:
            P.put(y + 1, bx, f"{rpm:>5} RPM", attr)
            P.put(y + 1, bx + 11, clip(f"{T.hz:.1f} Hz  {status[0]}", iw - 11), status[1])
        gmax = self.gauge_max()
        if foot:
            gy = y + h - 1 - foot
            P.bar(gy, bx, iw - 6, T.rpm / gmax, attr=C["pri"])
            P.put(gy, bx + iw - 5, f"{min(999, T.rpm / gmax * 100):4.0f}%", C["txt"])
            if foot == 2:
                P.spark(gy + 1, bx, iw, T.history, vmax=gmax, attr=C["acc"])

    def box_thermal(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        P.box(y, x, h, w, "THERMAL", tag=f"warn {self.args.temp_warn:.0f}° crit {self.args.temp_crit:.0f}°")
        t, iw = S.temp, w - 4
        if t is None:
            P.put(y + 1, x + 2, "no thermal sensor found", C["dim"])
            return
        a = C["crit"] if t >= self.args.temp_crit else C["warn"] if t >= self.args.temp_warn else C["ok"]
        P.put(y + 1, x + 2, f"{t:5.1f}°C", a | BOLD)
        P.bar(y + 1, x + 11, iw - 9, (t - 25.0) / 65.0, attr=a)
        if h >= 4:
            P.spark(y + 2, x + 2, iw, S.temp_hist, vmax=90.0, vmin=25.0, attr=a)

    def box_net_mini(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        ifs = self.visible_ifaces()
        P.box(y, x, h, w, "NETWORK", tag=f"SSID {S.ssid}" if S.ssid else None)
        iw = w - 4
        rows = h - 3
        if not ifs:
            P.put(y + 1, x + 2, "no active interfaces", C["dim"])
        for i, n in enumerate(ifs[:rows]):
            ip = S.ips.get(n, "--")
            P.put(y + 1 + i, x + 2, f"{n:<8.8}", C["pri"])
            P.put(y + 1 + i, x + 11, clip(ip, iw - 9), C["txt"] if ip != "--" else C["dim"])
        ly = y + h - 2
        rates = f"▼{fmt_bytes(S.rx_rate):>7} ▲{fmt_bytes(S.tx_rate):>7}"
        P.put(ly, x + 2, rates, C["txt"])
        P.spark(ly, x + 3 + len(rates), iw - len(rates) - 1, S.rx_hist, attr=C["pri"])

    def box_top(self, y, x, h, w):
        P, C = self.P, self.C
        P.box(y, x, h, w, "TOP PROCESSES", tag=f"{self.procs.total} TASKS")
        iw = w - 4
        P.put(y + 1, x + 2, clip(f"{'PID':>6} {'CPU%':>5} {'MEM%':>5}  NAME", iw), C["dim"])
        for i, r in enumerate(self.procs.top("cpu", h - 3)):
            a = C["crit"] if r[2] >= 50 else C["warn"] if r[2] >= 15 else C["txt"]
            P.put(y + 2 + i, x + 2, clip(f"{r[0]:>6} {r[2]:5.1f} {r[3]:5.1f}  {r[7]}", iw), a)

    def page_fan(self, y0, h, w):
        P, C, T = self.P, self.C, self.tach
        st, sa = self.tach_status()
        phys = PHYS.get(T.pin, "?")
        title = f"TACHOMETER // GPIO{T.pin} · PIN {phys}"
        if not T.online:
            P.box(y0, 0, h, w, title, tag=st, tag_attr=sa)
            lines = [(f"NO SIGNAL ON GPIO{T.pin}", C["crit"] | BOLD), ("", 0)]
            for seg in wrap("error: " + (T.error or "unknown"), w - 6)[:4]:
                lines.append((seg, C["warn"]))
            lines += [("", 0), ("CHECKLIST", C["pri"] | BOLD),
                      (f" • Noctua tach wire (GREEN) → GPIO{T.pin} = physical pin {phys}", C["txt"]),
                      (" • Fan ground (BLACK) → any Pi GND pin, e.g. physical pin 6", C["txt"])]
            if T.pin in (2, 3):
                lines.append((f" • GPIO{T.pin} doubles as I2C: keep I2C off "
                              "(sudo raspi-config → Interface Options → I2C → No)", C["txt"]))
            lines += [(" • lgpio must be installed:  sudo apt install python3-lgpio", C["txt"]),
                      (" • Using a venv? create it with --system-site-packages so lgpio is visible", C["txt"]),
                      ("", 0),
                      ("Preview without hardware:  python3 cyberdeck.py --sim", C["dim"])]
            for i, (text, a) in enumerate(lines[:h - 2]):
                P.put(y0 + 1 + i, 3, clip(text, w - 6), a)
            return

        wide = w >= 76
        ns = 0 if wide else 2                       # stats lines under the digits on narrow screens
        stats_x = w - 36
        rpm = int(round(T.rpm))
        txt = (f"{rpm:>4d}" if rpm < 10000 else str(rpm)).replace(" ", "_")
        attr = C["crit"] | BOLD if T.stalled else C["pri"] | BOLD
        # the readout grows with the screen but always leaves room for the graph
        max_th = min(h - 6, max(9 + ns, int(h * 0.55))) if h >= 15 else h
        avail_w = (stats_x - 4 if wide else w - 6) - 10          # 10 = rotor column
        fit = pix_fit(txt, DIGITS, avail_w, max_th - ns - 6, DIGIT_LADDER) or \
            pix_fit(txt, DIGITS, avail_w, max_th - ns - 4, DIGIT_LADDER)
        dr = pix_size(txt, DIGITS, *fit)[1] if fit else 1
        pad = 1 if dr + ns + 6 <= max_th else 0
        th = min(h, dr + ns + 4 + 2 * pad)
        P.box(y0, 0, th, w, title, tag=st, tag_attr=sa)
        dy = y0 + 1 + pad
        if fit:
            bw = self.draw_big(dy, 3, txt, attr, *fit)
        else:
            P.put(dy, 3, f"{rpm} RPM", attr)
            bw = len(f"{rpm} RPM")
        ly = dy + dr
        if fit:
            P.put(ly, 3, "REVOLUTIONS / MIN", C["dim"])
        rx = 3 + bw + 4
        if fit and dr >= 3 and rx + 6 < (stats_x if wide else w - 2):
            ry = dy + (dr - 3) // 2
            for r, s in enumerate(ROTOR[int(self.rotor) % 2]):
                P.ink(ry + r, rx, s, C["acc"] | BOLD if T.rpm > 0 else C["dim"])
            P.put(ly, rx, "STALL" if T.stalled else " SPIN", C["crit"] | BOLD if T.stalled else C["ok"])

        def f(v):
            return "--" if v is None else f"{v:.0f}"
        mam = f"{f(T.rpm_min)} / {f(T.rpm_avg)} / {f(T.rpm_max) if T._n else '--'}"
        if wide:
            stats = [("FREQUENCY", f"{T.hz:.1f} Hz"), ("PULSES", f"{T.total_pulses:,}"),
                     ("MIN/AVG/MAX", mam), ("PULSES/REV", f"{T.ppr}   window {T.window_s:.1f}s"),
                     ("BACKEND", f"{T.backend} {T.detail}")]
            sy = y0 + 1 + max(0, (th - 4 - len(stats)) // 2)
            for i, (lab, val) in enumerate(stats[:max(0, th - 4)]):
                P.put(sy + i, stats_x, f"{lab:<12}", C["dim"])
                P.put(sy + i, stats_x + 12, clip(val, w - stats_x - 14), C["txt"])
        else:
            P.put(ly + 1, 3, clip(f"{T.hz:.1f} Hz · {T.total_pulses:,} pulses · {T.backend}", w - 6), C["txt"])
            P.put(ly + 2, 3, clip(f"min/avg/max {mam}", w - 6), C["txt"])
        gmax = self.gauge_max()
        auto = "" if self.args.max_rpm > 0 else " (auto)"
        gl = f" {T.rpm / gmax * 100:3.0f}% of {gmax}{auto}" if w >= 60 else f" {T.rpm / gmax * 100:3.0f}%"
        gy = y0 + th - 2
        P.put(gy, 3, "SPEED", C["dim"])
        P.bar(gy, 9, w - 12 - len(gl), T.rpm / gmax, attr=C["acc"] if not T.stalled else C["crit"])
        P.put(gy, w - 3 - len(gl), gl, C["txt"])

        gy, gh = y0 + th, h - th
        if gh < 5:
            return
        gx = 9
        gw = w - gx - 2
        hist = list(T.history)
        win = hist[-gw:]
        vmax = max(500, int(math.ceil(max(win or [0]) * 1.15 / 500.0)) * 500)
        P.box(gy, 0, gh, w, "RPM HISTORY", tag=f"last {len(win)}s · 1 sample/s")
        ih = gh - 3
        for r in range(ih):
            P.put(gy + 1 + r, gx - 1, "┤" if r in (0, ih // 2, ih - 1) else "│", C["frame"])
        P.put(gy + 1, 2, f"{vmax:>5}", C["dim"])
        P.put(gy + 1 + ih // 2, 2, f"{vmax // 2:>5}", C["dim"])
        P.put(gy + ih, 2, f"{0:>5}", C["dim"])
        P.graph(gy + 1, gx, ih, gw, win, vmax, self.grad)
        P.put(gy + gh - 2, gx - 1, "└" + "─" * gw, C["frame"])
        P.put(gy + gh - 2, gx + 1, f" -{gw}s ", C["dim"])
        P.put(gy + gh - 2, gx + gw - 6, " now ", C["dim"])

    # ---- NET ---------------------------------------------------------------
    def page_net(self, y0, h, w):
        nrows = max(1, min(len(self.visible_ifaces()), 5))
        if w >= 80:
            self.vstack(y0, 0, w, h, [(4, max(6, nrows + 3), 0, False, self.net_top),
                                      (4, 10, 2, True, self.net_traffic),
                                      (3, 7, 1, False, self.net_diag)])
        else:
            self.vstack(y0, 0, w, h, [(4, nrows + 3, 0, False, self.net_ifaces),
                                      (3, 6, 3, False, self.net_link),
                                      (4, 10, 2, True, self.net_traffic),
                                      (3, 7, 1, False, self.net_diag)])

    def net_top(self, y, x, h, w):
        lw = (w * 3) // 5
        self.net_ifaces(y, x, h, lw)
        self.net_link(y, x + lw, h, w - lw)

    def net_ifaces(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        ifs = self.visible_ifaces()
        P.box(y, x, h, w, "INTERFACES", tag=f"{len(ifs)} LINKS")
        iw = w - 4
        if iw >= 43:
            cols = [("IFACE", 9, "<"), ("IPv4", 16, "<"), ("▼ RX/s", 9, ">"), ("▲ TX/s", 9, ">")]
            if iw >= 63:
                cols += [("▼ TOTAL", 10, ">"), ("▲ TOTAL", 10, ">")]
        else:
            cols = [("IFACE", 7, "<"), ("IPv4", 15, "<"), ("▼RX", 7, ">"), ("▲TX", 7, ">")]
        P.put(y + 1, x + 2, clip("".join(f"{t:{al}{wd}}" for t, wd, al in cols), iw), C["dim"])
        for i, n in enumerate(ifs[:max(0, h - 3)]):
            d = S.net[n]
            ip = S.ips.get(n, "--")
            vals = [n[:cols[0][1] - 1], ip, fmt_bytes(d["rxr"]), fmt_bytes(d["txr"]),
                    fmt_bytes(d["rx"]), fmt_bytes(d["tx"])]
            row = "".join(f"{v:{al}{wd}}" for v, (_, wd, al) in zip(vals, cols))
            P.put(y + 2 + i, x + 2, clip(row, iw), C["txt"] if ip != "--" else C["dim"])
        if not ifs and h > 3:
            P.put(y + 2, x + 2, "no active interfaces", C["dim"])

    def net_link(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        P.box(y, x, h, w, "LINK", tag="WIFI" if S.wifi else "WIRED" if S.ips else "DOWN")
        iw = w - 4
        rows = [("HOST", S.hostname, C["txt"]),
                ("SSID", S.ssid or "--", C["acc"] | BOLD if S.ssid else C["dim"]),
                ("GATEWAY", S.gateway or "--", C["txt"] if S.gateway else C["dim"])]
        for i, (lab, val, a) in enumerate(rows[:max(0, h - 2)]):
            P.put(y + 1 + i, x + 2, f"{lab:<8}", C["dim"])
            P.put(y + 1 + i, x + 10, clip(val, iw - 8), a)
        sy = y + 1 + len(rows)
        if sy < y + h - 1:
            P.put(sy, x + 2, "SIGNAL", C["dim"])
            if S.wifi:
                q, lvl = S.wifi
                lab = f"{lvl:.0f}dBm"
                P.put(sy, x + 10, lab, C["txt"])
                P.bar(sy, x + 11 + len(lab), iw - 9 - len(lab), q / 70.0, attr=P.level(1 - q / 70.0, 0.4, 0.7))
            else:
                P.put(sy, x + 10, "--", C["dim"])

    def net_traffic(self, y, x, h, w):
        P, C, S = self.P, self.C, self.sys
        P.box(y, x, h, w, "TRAFFIC", tag=f"▼ {fmt_bytes(S.rx_rate)}/s   ▲ {fmt_bytes(S.tx_rate)}/s")
        inner = h - 2
        half = max(1, inner // 2)
        gx, gw = x + 12, w - 14
        rx = list(S.rx_hist)[-gw:]
        tx = list(S.tx_hist)[-gw:]
        vrx = max(max(rx or [0]), 1024.0)
        vtx = max(max(tx or [0]), 1024.0)
        P.put(y + 1, x + 2, "▼ RX", C["pri"] | BOLD)
        if half >= 2:
            P.put(y + 2, x + 2, f"{fmt_bytes(vrx)}/s", C["dim"])
        P.graph(y + 1, gx, half, gw, rx, vrx, C["pri"])
        if inner - half >= 1:
            P.put(y + 1 + half, x + 2, "▲ TX", C["acc"] | BOLD)
            if inner - half >= 2:
                P.put(y + 2 + half, x + 2, f"{fmt_bytes(vtx)}/s", C["dim"])
            P.graph(y + 1 + half, gx, inner - half, gw, tx, vtx, C["acc"])

    def net_diag(self, y, x, h, w):
        P, C, dg = self.P, self.C, self.diag
        P.box(y, x, h, w, "DIAGNOSTICS", tag="PROBING…" if dg.running else "[D] RUN",
              tag_attr=C["warn"] | BOLD if dg.running else C["dim"])
        room = h - 2
        res = list(dg.results)
        if not res and not dg.running:
            for i, seg in enumerate(wrap(f"Press D to probe the gateway, the uplink ({self.args.ping_host}) "
                                         "and DNS.", w - 4)[:room]):
                P.put(y + 1 + i, x + 2, seg, C["dim"])
        for i, (lab, text, lvl) in enumerate(res[:room]):
            P.put(y + 1 + i, x + 2, f"{lab:<9}", C["pri"])
            P.put(y + 1 + i, x + 12, clip(text, w - 14), C.get(lvl, C["txt"]) | (BOLD if lab == "VERDICT" else 0))
        if dg.running and len(res) < room:
            P.put(y + 1 + len(res), x + 2, "◐◓◑◒"[self.frame % 4] + " probing…", C["warn"])
        elif dg.last_run and not dg.running and len(res) < room:
            P.put(y + 1 + len(res), x + 2,
                  f"last run {time.strftime('%H:%M:%S', time.localtime(dg.last_run))}", C["dim"])

    # ---- PROC ----------------------------------------------------------------
    def page_proc(self, y0, h, w):
        P, C, S, PM = self.P, self.C, self.sys, self.procs
        P.box(y0, 0, h, w, "PROCESS MATRIX", tag=f"SORT {self.proc_sort.upper()} · [S]")
        iw = w - 4
        l1, l5, l15 = S.load
        P.put(y0 + 1, 2, clip(f"TASKS {PM.total}   RUNNING {PM.running}   THREADS {PM.threads}   "
                              f"LOAD {l1:.2f} {l5:.2f} {l15:.2f}", iw), C["pri"])
        full = iw >= 60
        wide = iw >= 96
        if full:
            hdr = f"{'PID':>7} {'USER':<10} {'CPU%':>6} {'MEM%':>6} {'RSS':>7} S {'THR':>4}  "
            hdr += (f"{'LOAD':<12}" if wide else "") + "COMMAND"
        else:
            hdr = f"{'PID':>6} {'CPU%':>5} {'MEM%':>5}  COMMAND"
        P.put(y0 + 2, 2, hdr.ljust(iw)[:iw], C["hdr"])
        for i, r in enumerate(PM.top(self.proc_sort, h - 4)):
            pid, user, cpu, memp, rss, state, nthr, name = r
            a = C["crit"] if cpu >= 50 else C["warn"] if cpu >= 15 else C["txt"] if cpu >= 1 else C["dim"]
            if full:
                line = f"{pid:>7} {user[:10]:<10} {cpu:6.1f} {memp:6.1f} {fmt_bytes(rss):>7} {state} {nthr:>4}  "
            else:
                line = f"{pid:>6} {cpu:5.1f} {memp:5.1f}  "
            P.put(y0 + 3 + i, 2, line, a)
            cx = 2 + len(line)
            if wide:
                P.bar(y0 + 3 + i, cx, 10, cpu / 100.0)
                cx += 12
            P.put(y0 + 3 + i, cx, clip(name, iw - cx + 2), C["pri"] if cpu >= 1 else C["dim"])

    # ---- TOOLS ---------------------------------------------------------------
    def tools_top_h(self, bw, h):
        """Height of the chrono/timer row: digits grow with the screen."""
        max_th = min(h - 5, max(9, int(h * 0.55))) if h >= 14 else h
        fit = pix_fit("00:00", DIGITS, bw - 9, max_th - 6, DIGIT_LADDER)
        dr = pix_size("00:00", DIGITS, *fit)[1] if fit else 1
        return max(3, min(h, dr + 6))

    def page_tools(self, y0, h, w):
        if w >= 80:
            half = w // 2
            th = self.tools_top_h(half, h)
            self.box_chrono(y0, 0, th, half)
            self.box_timer(y0, half, th, w - half)
            bh = h - th
            if bh >= 3:
                nw = (w * 3) // 5
                self.box_notes(y0 + th, 0, bh, nw)
                self.box_keygen(y0 + th, nw, bh, w - nw)
        else:
            want = self.tools_top_h(w, max(9, h // 2))
            self.vstack(y0, 0, w, h, [(3, want, 0, False, self.box_chrono),
                                      (3, want, 1, False, self.box_timer),
                                      (3, 8, 3, True, self.box_notes),
                                      (3, 8, 2, False, self.box_keygen)])

    def box_chrono(self, y, x, h, w):
        P, C, sw = self.P, self.C, self.sw
        e = sw.elapsed()
        P.box(y, x, h, w, "CHRONOGRAPH",
              tag="RUNNING" if sw.running else ("HOLD" if e else "READY"),
              tag_attr=C["ok"] | BOLD if sw.running else C["dim"])
        if e < 6000:
            big, sub = f"{int(e // 60):02d}:{int(e % 60):02d}", f".{int(e * 10) % 10}"
        else:
            big, sub = f"{int(e // 3600):02d}:{int(e % 3600 // 60):02d}", "h"
        a = C["pri"] | BOLD if sw.running else C["txt"]
        inner = h - 2
        fit = pix_fit(big, DIGITS, w - 9, inner - 4, DIGIT_LADDER) if inner >= 7 else None
        if fit:
            bw = self.draw_big(y + 2, x + 3, big, a, *fit)
            dh = pix_size(big, DIGITS, *fit)[1]
            P.put(y + 1 + dh, x + 4 + bw, sub, C["acc"] | BOLD)
            iy = y + 3 + dh
        else:
            P.put(y + 1, x + 3, big + sub, a)
            iy = y + 2
        laps = sw.laps[-3:]
        if laps:
            base = len(sw.laps) - len(laps)
            lap_txt = "LAPS " + "  ".join(f"{base + i + 1}) {int(v // 60):02d}:{v % 60:04.1f}"
                                         for i, v in enumerate(laps))
        else:
            lap_txt = "LAPS --"
        for yy, text, at in ((iy, lap_txt, C["txt"] if laps else C["dim"]),
                             (iy + 1, "[SPC] start/stop  [L] lap  [Z] zero", C["dim"])):
            if yy < y + h - 1:
                P.put(yy, x + 3, clip(text, w - 5), at)

    def box_timer(self, y, x, h, w):
        P, C, T = self.P, self.C, self.timer
        left = T.left()
        state = "RUNNING" if T.running else "DONE" if T.done else "PAUSED" if left < T.set_s else "READY"
        P.box(y, x, h, w, "COUNTDOWN", tag=f"SET {T.set_s // 60} MIN",
              tag_attr=C["acc"] if T.running else C["dim"])
        hot = T.running and left <= 10 and self.frame % 10 < 5
        a = C["crit"] | BOLD if hot or T.done else C["acc"] | BOLD if T.running else C["txt"]
        sa = C["ok"] | BOLD if T.running else C["warn"] if T.done else C["dim"]
        big = f"{int(left // 60):02d}:{int(left % 60):02d}"
        inner = h - 2
        fit = pix_fit(big, DIGITS, w - 6, inner - 4, DIGIT_LADDER) if inner >= 7 else None
        if fit:
            self.draw_big(y + 2, x + 3, big, a, *fit)
            iy = y + 2 + pix_size(big, DIGITS, *fit)[1]
            P.put(iy, x + 3, state, sa)
            iy += 1
        else:
            P.put(y + 1, x + 3, big, a)
            P.put(y + 1, x + 4 + len(big), state, sa)
            iy = y + 2
        if iy < y + h - 1:
            P.bar(iy, x + 3, w - 6, 1 - left / T.set_s if T.set_s else 0, attr=C["acc"])
        if iy + 1 < y + h - 1:
            P.put(iy + 1, x + 3, clip("[T] go/pause  [+/-] min  [R] reset", w - 5), C["dim"])

    def box_notes(self, y, x, h, w):
        P, C = self.P, self.C
        P.box(y, x, h, w, "NOTES", tag=f"{len(self.notes.items)} SAVED · [N] NEW")
        room = h - 2
        items = self.notes.items[-room:] if room > 0 else []
        if not items:
            for i, text in enumerate(["No notes yet. Press N to jot one down.", f"saved to {NOTES_FILE}"][:room]):
                P.put(y + 1 + i, x + 2, clip(text, w - 4), C["dim"])
        for i, line in enumerate(items):
            stamp, body = line[:16], line[16:].strip()
            newest = i == len(items) - 1
            P.put(y + 1 + i, x + 2, stamp, C["acc"] if newest else C["dim"])
            P.put(y + 1 + i, x + 19, clip(body, w - 21), C["txt"] | (BOLD if newest else 0))

    def box_keygen(self, y, x, h, w):
        P, C, K = self.P, self.C, self.keys
        P.box(y, x, h, w, "KEYGEN", tag="[G] NEW")
        rows = [("PASS", K.password, C["acc"] | BOLD), ("PIN", K.pin, C["pri"] | BOLD),
                ("HEX", K.hex, C["pri"]), ("UUID", K.uuid, C["txt"]),
                ("D20", str(K.d20), C["warn"] | BOLD), ("COIN", K.coin, C["warn"])]
        for i, (lab, val, a) in enumerate(rows[:max(0, h - 2)]):
            P.put(y + 1 + i, x + 2, f"{lab:<5}", C["dim"])
            P.put(y + 1 + i, x + 7, clip(val, w - 9), a)

    # ---- TERM ----------------------------------------------------------------
    def page_term(self, y0, h, w):
        log_want = max(5, min(10, h // 3))
        self.vstack(y0, 0, w, h, [(3, max(3, h - log_want), 0, True, self.term_console),
                                  (3, log_want, 1, False, self.term_log)])

    def term_console(self, y, x, h, w):
        P, C, con = self.P, self.C, self.console
        tag = "RUNNING · [K] KILL" if con.running else con.short_cwd()
        if con.scroll:
            tag = f"SCROLLED -{con.scroll} · [END]"
        P.box(y, x, h, w, "CONSOLE", tag=tag, tag_attr=C["warn"] | BOLD if con.running else C["dim"])
        iw, vis = w - 4, h - 2
        disp = []
        for text, kind in list(con.lines):
            for seg in wrap(text, iw):
                disp.append((seg, kind))
        con.max_scroll = max(0, len(disp) - vis)
        con.scroll = min(con.scroll, con.max_scroll)
        end = len(disp) - con.scroll
        window = disp[max(0, end - vis):end]
        kinds = {"cmd": C["acc"] | BOLD, "out": C["txt"], "err": C["crit"], "sys": C["dim"]}
        for i, (seg, kind) in enumerate(window):
            P.put(y + 1 + i, x + 2, seg, kinds.get(kind, C["txt"]))
        if con.running and not con.scroll and len(window) < vis:
            P.put(y + 1 + len(window), x + 2, "◐◓◑◒"[self.frame % 4], C["warn"])

    def term_log(self, y, x, h, w):
        P, C = self.P, self.C
        P.box(y, x, h, w, "EVENT LOG", tag=f"{len(self.log.items)} EVENTS")
        lv = {"info": C["pri"], "ok": C["ok"], "warn": C["warn"], "crit": C["crit"] | BOLD, "sys": C["dim"]}
        for i, (ts, lvl, msg) in enumerate(list(self.log.items)[-max(1, h - 2):]):
            P.put(y + 1 + i, x + 2, ts, C["dim"])
            P.put(y + 1 + i, x + 11, f"{lvl.upper():<4}", lv.get(lvl, C["txt"]))
            P.put(y + 1 + i, x + 16, clip(msg, w - 18),
                  lv.get(lvl, C["txt"]) if lvl in ("crit", "warn") else C["txt"])

    # ---- PENTEST -------------------------------------------------------------
    PT_SECTIONS = ["RECON", "WIRELESS", "KIT", "REF"]

    def page_pentest(self, y0, h, w):
        P, C = self.P, self.C
        self.pt_row = y0
        # sub-tab row
        P.put(y0, 2, "SECTION", C["dim"])
        x = 11
        self.pt_hits = []
        for i, name in enumerate(self.PT_SECTIONS):
            lab = f" {name} "
            P.put(y0, x, lab, C["hdr2"] if i == self.pentest_mode else C["pri"])
            self.pt_hits.append((x, x + len(lab), i))
            x += len(lab) + 1
        P.put(y0, max(x + 1, w - len(AUTHZ) - 2), clip("⚠ " + AUTHZ, w - x - 3), C["warn"])
        body_y, body_h = y0 + 1, h - 1
        [self._pt_recon, self._pt_wireless, self._pt_kit, self._pt_ref][self.pentest_mode](body_y, body_h, w)

    def _spin(self):
        return "◐◓◑◒"[self.frame % 4]

    def _pt_recon(self, y, h, w):
        P, C, R = self.P, self.C, self.recon
        tag = f"SCANNING {self._spin()}" if R.scanning else "[S] HOSTS  [F] +PORTS"
        P.box(y, 0, h, w, "LAN RECON · your own subnet",
              tag=tag, tag_attr=C["warn"] | BOLD if R.scanning else C["dim"])
        iw = w - 4
        yy = y + 1
        if R.target:
            P.put(yy, 2, clip(f"target {R.target}   via {R.method}", iw), C["pri"])
            yy += 1
        if R.scanning:
            P.put(yy, 2, clip(f"{self._spin()} {R.phase}…", iw), C["warn"])
            yy += 1
        if R.error:
            P.put(yy, 2, clip("! " + R.error, iw), C["crit"])
            yy += 1
        if not R.hosts and not R.scanning and not R.error:
            for i, seg in enumerate(["Press S for a host sweep of the subnet this device is on",
                                     "(nmap / arp-scan if installed, else a built-in ping sweep).",
                                     "F also scans common TCP ports on each host found."]):
                if yy + i < y + h - 1:
                    P.put(yy + i, 2, clip(seg, iw), C["dim"])
            return
        if R.hosts:
            hdr = f"{'IP':<16}{'MAC':<19}{'VENDOR / PORTS'}"
            P.put(yy, 2, clip(hdr, iw), C["hdr"])
            yy += 1
            for hst in R.hosts[:y + h - 2 - yy]:
                P.put(yy, 2, f"{hst['ip']:<16}", C["acc"] | BOLD)
                P.put(yy, 18, f"{(hst['mac'] or '--'):<19}", C["txt"] if hst["mac"] else C["dim"])
                if hst["ports"]:
                    extra = ",".join(str(p) for p in hst["ports"]) or "none open"
                    P.put(yy, 37, clip("ports " + extra, iw - 35), C["ok"])
                else:
                    label = hst["vendor"] or hst["name"] or ("open: none" if hst["ports"] == [] else "")
                    P.put(yy, 37, clip(label, iw - 35), C["dim"])
                yy += 1
            if not R.scanning:
                P.put(y + h - 1 - 0, 2, "", 0)

    def _pt_wireless(self, y, h, w):
        P, C, W = self.P, self.C, self.wireless
        tag = f"SURVEYING {self._spin()}" if W.scanning else "[S] SCAN"
        P.box(y, 0, h, w, "WIRELESS SURVEY · read-only",
              tag=tag, tag_attr=C["warn"] | BOLD if W.scanning else C["dim"])
        iw = w - 4
        half = (h - 2) // 2
        # Wi-Fi (top half)
        P.put(y + 1, 2, f"▸ WI-FI  ({len(W.wifi)})" + (f"  {self._spin()} {W.phase}" if W.scanning and W.phase == "wifi" else ""),
              C["pri"] | BOLD)
        wy = y + 2
        if W.wifi_err:
            P.put(wy, 2, clip("! " + W.wifi_err, iw), C["crit"])
        elif not W.wifi and not W.scanning:
            P.put(wy, 2, "Press S to list nearby access points (SSID, signal, security).", C["dim"])
        else:
            P.put(wy, 2, clip(f"{'SIGNAL':<10}{'CH':<5}{'SECURITY':<17}SSID", iw), C["hdr"])
            for i, ap in enumerate(W.wifi[:max(0, half - 2)]):
                ry = wy + 1 + i
                sig = ap["signal"]
                bars = min(4, max(1, sig // 25 + (1 if sig % 25 else 0)))
                sa = C["ok"] if sig > 60 else C["warn"] if sig > 35 else C["dim"]
                P.put(ry, 2, "▁▃▅▇"[:bars] + "    "[bars:], sa | BOLD)
                P.put(ry, 6, f"{sig:>4}", sa)
                P.put(ry, 12, f"{ap['chan']:<4}", C["dim"])
                sec = "open" if ap["sec"] in ("", "open") else ap["sec"]
                P.put(ry, 17, f"{clip(sec, 16):<17}", C["warn"] if sec == "open" else C["txt"])
                P.put(ry, 34, clip(ap["ssid"], iw - 32), C["acc"] if ap["ssid"] != "<hidden>" else C["dim"])
        # Bluetooth (bottom half)
        by = y + 1 + half
        P.put(by, 2, f"▸ BLUETOOTH  ({len(W.bt)})" + (f"  {self._spin()} {W.phase}" if W.scanning and W.phase == "bluetooth" else ""),
              C["pri"] | BOLD)
        if W.bt_err:
            P.put(by + 1, 2, clip("! " + W.bt_err, iw), C["crit"])
        elif not W.bt and not W.scanning:
            P.put(by + 1, 2, "Nearby Bluetooth devices appear here after a scan.", C["dim"])
        else:
            for i, d in enumerate(W.bt[:max(0, (y + h - 1) - (by + 1))]):
                P.put(by + 1 + i, 2, f"{d['addr']}  ", C["txt"])
                P.put(by + 1 + i, 21, clip(d["name"], iw - 19), C["acc"])

    def _pt_kit(self, y, h, w):
        P, C, K = self.P, self.C, self.kit
        if not K.rows and not K.scanning:
            K.start()
        missing = K.missing_packages()
        if K.installing:
            tag, ta = f"INSTALLING {self._spin()}", C["warn"] | BOLD
        elif self._kit_arm:
            tag, ta = f"INSTALL {len(missing)} PKG? PRESS I", C["crit"] | BOLD
        elif K.done:
            extra = f" · [I] INSTALL {len(missing)}" if missing else ""
            tag, ta = f"{K.n_ok}/{len(KIT_TOOLS)} READY · [S] RECHECK{extra}", C["dim"]
        else:
            tag, ta = f"CHECKING {self._spin()}", C["warn"] | BOLD
        P.box(y, 0, h, w, "KIT INVENTORY · installed tools", tag=tag, tag_attr=ta)
        iw = w - 4
        rows = K.rows
        if not rows:
            P.put(y + 1, 2, f"{self._spin()} probing…", C["warn"])
            return
        # when there is install activity, give the lower third to a live apt log
        show_log = K.installing or K.install_log
        grid_h = (h - 2) - (max(5, (h - 2) // 3) + 1) if show_log else (h - 2)
        grid_h = max(1, grid_h)
        two = iw >= 74
        colw = iw // 2 if two else iw
        for idx, r in enumerate(rows):
            col = idx // grid_h if two else 0
            line = idx % grid_h if two else idx
            if col >= (2 if two else 1) or line >= grid_h:
                continue
            cx = 2 + col * (colw + 1)
            ry = y + 1 + line
            ok = bool(r["path"])
            P.put(ry, cx, "●" if ok else "○", C["ok"] | BOLD if ok else C["dim"])
            P.put(ry, cx + 2, f"{r['cmd']:<13}", C["txt"] if ok else C["dim"])
            if ok:
                P.put(ry, cx + 16, clip(r["what"], colw - 16), C["dim"])
            else:
                P.put(ry, cx + 16, clip(f"apt install {r['pkg']}", colw - 16), C["warn"])
        if self._kit_arm and not K.installing:
            msg = f"⚠ install {len(missing)} package(s) with sudo apt — press I to confirm, any other key cancels"
            P.put(y + h - 1, 2, clip(msg, iw), C["crit"] | BOLD)
            return
        if show_log:
            ly = y + 1 + grid_h
            P.put(ly, 2, "─" * iw, C["frame"])
            P.put(ly, 3, " apt log ", C["pri"])
            if K.installing:
                P.put(ly, iw - 12, f" {self._spin()} working ", C["warn"] | BOLD)
            lv = {"cmd": C["acc"] | BOLD, "err": C["crit"], "ok": C["ok"] | BOLD, "sys": C["pri"], "out": C["txt"]}
            log_rows = (y + h - 1) - (ly + 1)
            for i, (text, kind) in enumerate(list(K.install_log)[-log_rows:]):
                P.put(ly + 1 + i, 2, clip(text, iw), lv.get(kind, C["txt"]))

    def _pt_ref(self, y, h, w):
        P, C = self.P, self.C
        lw = w // 2 if w >= 72 else w
        P.box(y, 0, h, lw, "METHODOLOGY")
        iw = lw - 4
        yy = y + 1
        for name, desc in PENTEST_PHASES:
            if yy >= y + h - 1:
                break
            P.put(yy, 2, f"{name:<9}", C["acc"] | BOLD)
            P.put(yy, 11, clip(desc, iw - 9), C["txt"])
            yy += 1
        if yy < y + h - 1:
            P.put(yy + 0 if yy < y + h - 1 else yy, 2, "", 0)
        if lw < w:
            rx = lw
            P.box(y, rx, h, w - rx, "FIELD KIT")
            for i, item in enumerate(PENTEST_GEAR[:max(0, h - 2)]):
                P.put(y + 1 + i, rx + 2, clip("▫ " + item, w - rx - 4), C["txt"])

    # ---- SERIAL --------------------------------------------------------------
    def page_serial(self, y0, h, w):
        P, C, L = self.P, self.C, self.serial
        L.refresh_ports()
        if w >= 76:
            side = max(24, min(34, w // 3))
            self._ser_ports(y0, 0, h, side)
            self._ser_console(y0, side, h, w - side)
        else:
            ph = max(4, min(len(L.ports) + 4, h // 2))
            self._ser_ports(y0, 0, ph, w)
            self._ser_console(y0 + ph, 0, h - ph, w)

    def _ser_ports(self, y, x, h, w):
        P, C, L = self.P, self.C, self.serial
        st = ("LIVE", C["ok"] | BOLD) if L.connected else ("OFFLINE", C["dim"])
        P.box(y, x, h, w, "PORTS", tag=st[0], tag_attr=st[1])
        iw = w - 4
        yy = y + 1
        if not L.ports:
            P.put(yy, x + 2, "no USB serial ports", C["warn"])
            P.put(yy + 1, x + 2, clip("plug in a device,", iw), C["dim"])
            P.put(yy + 2, x + 2, clip("then press R", iw), C["dim"])
            yy += 4
        else:
            for i, p in enumerate(L.ports):
                if yy >= y + h - 6:
                    break
                here = i == L.sel
                live = L.connected and p == L.port
                mark = "▶" if here else " "
                a = C["acc"] | BOLD if here else C["txt"]
                P.put(yy, x + 2, f"{mark} {clip(p.replace('/dev/', ''), iw - 4)}", a)
                if live:
                    P.put(yy, x + w - 6, "LIVE", C["ok"] | BOLD)
                yy += 1
            yy += 1
        settings = [
            ("BAUD", str(L.baud), C["pri"] | BOLD),
            ("FRAME", "8N1", C["txt"]),
            ("EOL", L.eol_name, C["txt"]),
            ("VIEW", "HEX" if L.hexmode else "ASCII", C["txt"]),
            ("RX", fmt_bytes(L.rx_bytes), C["dim"]),
            ("TX", fmt_bytes(L.tx_bytes), C["dim"]),
        ]
        for lab, val, a in settings:
            if yy >= y + h - 1:
                break
            P.put(yy, x + 2, f"{lab:<6}", C["dim"])
            P.put(yy, x + 8, clip(val, iw - 6), a)
            yy += 1

    def _ser_console(self, y, x, h, w):
        P, C, L = self.P, self.C, self.serial
        if L.connected:
            tag, ta = f"{L.port.replace('/dev/', '')} @ {L.baud}", C["ok"] | BOLD
        elif L.error:
            tag, ta = "ERROR", C["crit"] | BOLD
        else:
            tag, ta = "[O] OPEN SELECTED", C["dim"]
        if L.scroll:
            tag, ta = f"SCROLLED -{L.scroll}", C["warn"]
        P.box(y, x, h, w, "SERIAL CONSOLE", tag=tag, tag_attr=ta)
        iw, vis = w - 4, h - 2
        disp = []
        for text, kind in L.render_lines():
            for seg in wrap(text, iw) or [""]:
                disp.append((seg, kind))
        L.max_scroll = max(0, len(disp) - vis)
        L.scroll = min(L.scroll, L.max_scroll)
        end = len(disp) - L.scroll
        window = disp[max(0, end - vis):end]
        kinds = {"rx": C["txt"], "tx": C["acc"] | BOLD, "sys": C["dim"]}
        if not disp and not L.connected:
            P.put(y + 1, x + 2, "Select a port on the left and press O to open it.", C["dim"])
            P.put(y + 2, x + 2, clip("Then press ENTER to type and send a line to the device.", iw), C["dim"])
        for i, (seg, kind) in enumerate(window):
            P.put(y + 1 + i, x + 2, seg, kinds.get(kind, C["txt"]))

    # ---- overlays ------------------------------------------------------------
    def panel(self, title, lines, attr=None):
        P = self.P
        if len(lines) + 2 > P.h - 2:
            lines = [ln for ln in lines if ln[0]]
        bw = min(P.w - 4, max(len(t) for t, _ in lines) + 6)
        bh = min(P.h - 2, len(lines) + 2)
        y, x = (P.h - bh) // 2, (P.w - bw) // 2
        P.fill(y, x, bh, bw)
        P.box(y, x, bh, bw, title, attr=self.C["acc"] if attr is None else attr)
        for i, (text, a) in enumerate(lines[:bh - 2]):
            P.put(y + 1 + i, x + 3, clip(text, bw - 5), a)

    def draw_help(self):
        C = self.C
        hd, tx, dm = C["acc"] | BOLD, C["txt"], C["dim"]
        self.panel("HELP // KEYMAP", [
            ("GLOBAL", hd),
            ("1-8  TAB  ← →    switch page (or click/tap a tab)", tx),
            ("! or :           run a shell command", tx),
            ("M                matrix screensaver", tx),
            ("P                power menu (reboot / shutdown)", tx),
            ("? or H           this help        Q  quit", tx),
            ("", tx),
            ("PAGES", hd),
            ("FAN    R  reset min/avg/max", tx),
            ("NET    D  run gateway / uplink / DNS checks", tx),
            ("PROC   S  sort by CPU or memory", tx),
            ("TOOLS  SPACE chrono · L lap · Z zero", tx),
            ("       T timer · +/- minutes · R reset", tx),
            ("       N new note · G new keys", tx),
            ("TERM   K kill · C clear · ↑↓ PgUp PgDn scroll", tx),
            ("PTEST  [ ] section · S scan · F host+ports · I install kit", tx),
            ("SERIAL [ ] port · O open · D close · ENTER send a line", tx),
            ("       B baud · E line-ending · X hex/ascii · W wipe", tx),
            ("", tx),
            ("press any key to close", dm),
        ])

    def draw_power(self):
        C = self.C
        self.panel("POWER", [
            ("", 0),
            ("[R]  REBOOT", C["warn"] | BOLD),
            ("[S]  SHUT DOWN", C["crit"] | BOLD),
            ("any other key cancels", C["dim"]),
            ("", 0),
            ("runs: sudo -n systemctl reboot|poweroff", C["dim"]),
        ], attr=C["crit"])

    def draw_alarm(self):
        C = self.C
        flash = (self.frame // 3) % 2 == 0
        a = (C["alert"] if flash else C["crit"]) | BOLD
        self.panel("COUNTDOWN", [
            ("", 0),
            ("▓▓▓  TIMER COMPLETE  ▓▓▓", a),
            ("", 0),
            ("press any key", C["dim"]),
        ], attr=C["crit"])

    def draw_saver(self, h, w):
        if self.matrix is None or (self.matrix.h, self.matrix.w) != (h, w):
            self.matrix = Matrix(h, w)
        self.matrix.step()
        self.matrix.draw(self.P, self.C)
        label = " CYBERDECK // STANDBY · ANY KEY TO WAKE "
        if w > len(label):
            self.P.put(h // 2, (w - len(label)) // 2, label, self.C["hdr"])


# --------------------------------------------------------------------------
# boot sequence
# --------------------------------------------------------------------------

def title_layout(w, h):
    """Logo cells plus where the title block sits for the current screen size."""
    cells, lw, lh = logo_cells(w, h)
    ly = max(0, (h - (lh + 7)) // 2)
    lx = max(0, (w - lw) // 2)
    return cells, lw, lh, ly, lx


def wave_attr(grad, c, lw, frame):
    """Colour for logo column c: a cyan -> magenta gradient that drifts over time."""
    n = len(grad)
    if n == 1:
        return grad[0]
    t = int(c / max(1, lw) * (n - 1) + frame * 0.35) % (2 * n - 2)
    return grad[t if t < n else 2 * n - 2 - t]


def draw_title(app, frame, prompt=None, glitch=False, sub_n=None, operator=True):
    """The title screen, laid out fresh for whatever size the terminal is now."""
    s, P, C = app.scr, app.P, app.C
    P.sync()
    h, w = P.h, P.w
    s.erase()
    cells, lw, lh, ly, lx = title_layout(w, h)
    shift = {}
    if glitch and lh > 1:
        for r in random.sample(range(lh), min(2, lh)):
            shift[r] = random.choice((-2, -1, 1, 2))
    if lh > 1:
        for r, c, _ in cells:
            P.put(ly + r + 1, lx + c + 1, "░", C["frame"])
    for r, c, ch in cells:
        P.put(ly + r, lx + c + shift.get(r, 0), ch, wave_attr(app.grad, c, lw, frame) | BOLD)

    def center(y, text, attr):
        text = clip(text, w - 2)
        P.put(y, max(0, (w - len(text)) // 2), text, attr)

    sub = "// NEURAL INTERFACE ONLINE //" if w >= 34 else "// ONLINE //"
    if sub_n is not None:
        sub = sub[:sub_n].ljust(len(sub))
    center(ly + lh + 2, sub, C["acc"] | BOLD)
    if operator:
        center(ly + lh + 3, f"OPERATOR {app.sys.user}@{app.sys.hostname}  ·  v{VERSION}", C["dim"])
    if prompt:
        center(ly + lh + 5, prompt[0], prompt[1])
    status_y = h - 1 if h < 16 else h - 2
    if status_y > ly + lh + 5:
        checks = app.post_results or app.post_checks()
        ok = sum(1 for chk in checks if chk[2] == "OK")
        center(status_y, f"{app.sys.model}  ·  {ok}/{len(checks)} CHECKS OK  ·  TACH {app.tach_status()[0]}",
               C["dim"])


def wait_for_enter(app, frame=0):
    """Hold on the title screen until Enter is pressed. Returns False on Q."""
    s, C = app.scr, app.C
    s.timeout(50)
    while True:
        w = app.P.w
        text = "▶  PRESS [ENTER] TO JACK IN  ◀" if w >= 36 else "PRESS ENTER" if w >= 15 else "ENTER"
        on = (frame // 8) % 2 == 0
        draw_title(app, frame, prompt=(f" {text} ", C["hdr"] if on else C["pri"] | BOLD),
                   glitch=random.random() < 0.04)
        s.refresh()
        try:
            k = s.get_wch()
        except curses.error:
            k = None
        if is_enter(k):
            return frame
        if k in ("q", "Q"):
            return None
        frame += 1


def crt_on(app, pause):
    """Old-monitor power-on: a line that stretches out, then static."""
    s, P, C = app.scr, app.P, app.C
    P.sync()
    h, w = P.h, P.w
    cy, cx = h // 2, w // 2
    for i in range(1, 21):
        half = max(1, int(cx * i / 20))
        s.erase()
        P.put(cy, cx - half, "━" * (2 * half), C["txt"] | BOLD)
        s.refresh()
        pause(0.012)
    for i in range(1, 6):
        s.erase()
        span = int((h // 2) * i / 5)
        for yy in range(cy - span, cy + span + 1):
            P.put(yy, 0, "".join(random.choice(" ░░▒") for _ in range(w)), C["dim"])
        s.refresh()
        pause(0.03)
    s.erase()
    s.refresh()
    pause(0.2)


def draw_post(app, pause=None, animate=True):
    """BIOS-style power-on self test, laid out for the current screen size.

    With animate=False everything is drawn instantly (used when Enter skips
    ahead, or when the window is resized). Returns (prompt_row, x, width).
    """
    s, P, C = app.scr, app.P, app.C
    if not animate:
        def pause(_t):
            return None
    P.sync()
    h, w = P.h, P.w
    pw = max(10, min(w - 2, 100))        # centred column, never wider than 100
    px = max(1, (w - pw) // 2)
    lab_w = 12 if pw >= 56 else 8
    tag_x = px + pw - 8
    top = 1 if h >= 16 else 0
    y = [top]

    def row():
        """Row for the next line. When the screen is full, scroll everything up
        like a real terminal (the bottom row stays free for the Enter prompt)."""
        if y[0] > h - 2:
            try:
                s.scrollok(True)
                s.scroll(y[0] - (h - 2))
            except curses.error:
                s.erase()
            finally:
                s.scrollok(False)
            y[0] = h - 2
        return y[0]

    def show():
        if animate:
            s.refresh()

    title = f"CYBERDECK BIOS v{VERSION}"
    cright = "NEURAL SYSTEMS (c) 2026"
    P.put(row(), px, clip(title, pw), C["pri"] | BOLD)
    if pw >= len(title) + len(cright) + 4:
        P.put(y[0], px + pw - len(cright), cright, C["dim"])
    y[0] += 1
    P.put(row(), px, clip(app.sys.model, pw), C["acc"])
    y[0] += 2
    show()
    pause(0.25)

    tags = {"OK": ("  OK  ", "ok"), "WARN": (" WARN ", "warn"), "FAIL": (" FAIL ", "crit"), "SIM": (" SIM  ", "warn")}
    app.post_results = app.post_checks()
    for label, value, status in app.post_results:
        if len(label) > lab_w:
            label = label.split()[0][:lab_w]
        line = f"{label:<{lab_w}} {clip(value, max(1, pw - lab_w - 12))}"
        r = row()
        if animate:
            step = max(3, len(line) // 12)
            for j in range(1, len(line) + 1, step):
                P.put(r, px, line[:j], C["txt"])
                P.put(r, px, label[:j], C["pri"])
                show()
                pause(0.004)
        P.put(r, px, line, C["txt"])
        P.put(r, px, label, C["pri"])
        dots_x = px + len(line) + 1
        P.put(r, dots_x, "." * max(0, tag_x - 1 - dots_x), C["dim"])
        t, key = tags.get(status, tags["OK"])
        P.put(r, tag_x, f"[{t}]", C[key] | BOLD)
        show()
        pause(0.07)
        y[0] += 1
    y[0] += 1

    mt = app.sys.mem.get("MemTotal", 0)
    r = row()
    if animate:
        for i in range(0, 26):
            P.put(r, px, f"MEMTEST {int(mt * i / 25):>9} KB", C["txt"])
            show()
            pause(0.012)
    P.put(r, px, f"MEMTEST {mt:>9} KB", C["txt"])
    P.put(r, tag_x, "[  OK  ]", C["ok"] | BOLD)
    y[0] += 2

    show_hex = pw >= 52
    bar_w = max(3, min(10, pw - 29))
    for mod in ("kernel.ice", "neural.link", "tach.daemon", "holo.render", "net.uplink", "sys.watchdog"):
        r = row()
        for p in (range(11) if animate else (10,)):
            filled = round(p / 10 * bar_w)
            bar = "█" * filled + "░" * (bar_w - filled)
            P.put(r, px, f"> LOAD {mod:<13}", C["pri"])
            P.put(r, px + 20, f"[{bar}] {p * 10:3d}%", C["acc"])
            if show_hex:
                P.put(r, px + 29 + bar_w, f"0x{random.getrandbits(32):08X}", C["dim"])
            show()
            pause(0.01)
        y[0] += 1
    return min(y[0] + 1, h - 1), px, pw


def wait_post(app, layout):
    """Hold on the finished POST screen until Enter. Returns False on Q."""
    s, P, C = app.scr, app.P, app.C
    s.timeout(50)
    frame = 0
    size = (P.h, P.w)
    while True:
        P.sync()
        if (P.h, P.w) != size:           # window resized: lay the POST out again
            size = (P.h, P.w)
            s.erase()
            layout = draw_post(app, animate=False)
        py, px, pw = layout
        text = ("▶ POST COMPLETE · PRESS [ENTER] TO BOOT" if pw >= 44 else
                "PRESS [ENTER] TO BOOT" if pw >= 25 else "ENTER ▶")
        on = (frame // 8) % 2 == 0
        P.put(py, px, " " * pw, 0)
        P.put(py, px, f" {text} ", C["hdr"] if on else C["pri"] | BOLD)
        P.put(py, px + len(text) + 3, "█" if on else " ", C["pri"])
        s.refresh()
        try:
            k = s.get_wch()
        except curses.error:
            k = None
        if is_enter(k):
            return True
        if k in ("q", "Q"):
            return False
        frame += 1


def boot_sequence(app):
    """Boot: CRT power-on + POST, wait for Enter, logo, wait for Enter again.

    Enter during an animation fast-forwards to the next Enter prompt, so
    nothing is ever skipped by accident. Everything is laid out from the
    current terminal size. Returns False if the user quit with Q.
    """
    s, P, C = app.scr, app.P, app.C
    frame = 0

    def poll():
        try:
            k = s.get_wch()
        except curses.error:
            return
        if is_enter(k):
            raise SkipBoot

    def pause(t):
        end = time.monotonic() + t
        while True:
            poll()
            r = end - time.monotonic()
            if r <= 0:
                return
            time.sleep(min(0.012, r))

    # -- stage 1: power-on self test -------------------------------------------
    s.timeout(0)
    try:
        crt_on(app, pause)
        layout = draw_post(app, pause)
    except SkipBoot:
        s.erase()
        layout = draw_post(app, animate=False)
    if not wait_post(app, layout):
        return False

    # -- stage 2: logo decrypt -------------------------------------------------
    s.timeout(0)
    try:
        s.erase()
        s.refresh()
        pause(0.15)
        P.sync()
        h, w = P.h, P.w
        cells, lw, lh, ly, lx = title_layout(w, h)
        lock = {(r, c): c / max(1, lw) * 0.75 + random.uniform(0, 0.25) for r, c, _ in cells}
        junk = "▓▒░<>/\\|01#$%&*+=?"
        t0 = time.monotonic()
        while True:
            el = (time.monotonic() - t0) / 1.4
            s.erase()
            for r, c, ch in cells:
                if el >= lock[(r, c)]:
                    P.put(ly + r, lx + c, ch, wave_attr(app.grad, c, lw, 0) | BOLD)
                else:
                    P.put(ly + r, lx + c, random.choice(junk), C["dim"] if random.random() < 0.7 else C["acc"])
            s.refresh()
            if el >= 1.05:
                break
            pause(0.03)
        for n in range(0, 31, 2):
            draw_title(app, frame, sub_n=n, operator=False)
            s.refresh()
            frame += 1
            pause(0.03)
        for _ in range(3):
            draw_title(app, frame, glitch=True)
            s.refresh()
            pause(0.05)
            draw_title(app, frame)
            s.refresh()
            frame += 1
            pause(0.08)
    except SkipBoot:
        pass

    # -- stage 3: title screen, wait for the operator -------------------------
    frame = wait_for_enter(app, frame)
    if frame is None:
        return False
    for i in range(6):
        draw_title(app, frame + i, prompt=("  ACCESS GRANTED  ", C["hdr"] if i % 2 == 0 else C["ok"] | BOLD))
        s.refresh()
        time.sleep(0.11)
    time.sleep(0.25)

    # -- wipe --------------------------------------------------------------------
    P.sync()
    h, w = P.h, P.w
    step = max(1, h // 30)
    for yy in range(0, h + 2, step):
        for k in range(step):
            P.put(yy + k, 0, "▓" * w, C["frame"])
            P.put(yy + k - step, 0, "▒" * w, C["dim"])
            P.put(yy + k - 2 * step, 0, " " * w, 0)
        s.refresh()
        time.sleep(0.006)
    s.erase()
    s.refresh()
    return True


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(stdscr, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    if not args.no_mouse:
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
    app = App(stdscr, args)
    if not args.no_boot and not boot_sequence(app):
        app.tach.close()          # quit from the title screen
        return
    app.run()


def cli():
    p = argparse.ArgumentParser(description="CYBERDECK - neon terminal command deck for Raspberry Pi")
    p.add_argument("--pin", type=int, default=2, help="BCM GPIO number of the fan tach wire (default 2)")
    p.add_argument("--ppr", type=int, default=2, help="tach pulses per revolution (Noctua = 2)")
    p.add_argument("--baud", type=int, default=115200, help="default serial-console baud rate")
    p.add_argument("--max-rpm", type=int, default=0, help="full-scale RPM for the speed gauge (default: auto)")
    p.add_argument("--debounce", type=int, default=0, metavar="US",
                   help="ignore tach glitches shorter than this many microseconds (try 100 if RPM reads high)")
    p.add_argument("--sim", action="store_true", help="simulate a fan instead of reading GPIO")
    p.add_argument("--no-boot", action="store_true", help="skip the startup animation and both press-Enter screens")
    p.add_argument("--no-mouse", action="store_true", help="don't capture mouse/touch (keeps terminal text selection)")
    p.add_argument("--saver", type=float, default=0, metavar="MIN", help="start matrix screensaver after MIN idle minutes")
    p.add_argument("--timer", type=int, default=5, metavar="MIN", help="default countdown length in minutes")
    p.add_argument("--temp-warn", type=float, default=70.0, help="CPU temp warning threshold °C")
    p.add_argument("--temp-crit", type=float, default=80.0, help="CPU temp alarm threshold °C")
    p.add_argument("--ping-host", default="1.1.1.1", help="host used by the network diagnostics")
    args = p.parse_args()
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        curses.wrapper(main, args)
    except KeyboardInterrupt:
        pass
    print("CYBERDECK offline.")


if __name__ == "__main__":
    cli()
