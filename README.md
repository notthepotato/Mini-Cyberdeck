# CYBERDECK

A neon terminal command deck for your Raspberry Pi, with a live tachometer
for the Noctua fan on GPIO2.

## Run it

Open a terminal and run:

    cd ~/Documents/UI
    python3 cyberdeck.py

Other ways to run it:

    python3 cyberdeck.py --sim        # fake fan signal, to preview without hardware
    python3 cyberdeck.py --no-boot    # skip the startup animation and title screen
    python3 cyberdeck.py --help       # all options

Startup has two stops, and each waits for you to press **Enter**:

1. **Hardware check:** the BIOS-style list of checks ends with
   `PRESS [ENTER] TO BOOT`.
2. **Title screen:** the CYBERDECK logo shows `PRESS [ENTER] TO JACK IN`.

Pressing Enter while either animation is still playing jumps to that stage's
prompt, so you never skip a stage by accident. **Q** at either prompt exits.

Any terminal size works, from a small 40x12 screen up to a full-screen
monitor. The logo, the RPM readout and the timers grow with the window. On
small screens, panels shrink or drop out in order of importance. You can
resize the window while it's running.

## Boot straight into CYBERDECK (no desktop)

Run this once in a terminal on the Pi (it needs internet and asks for your password):

    bash ~/Documents/UI/install-autostart.sh

After a reboot, the Pi skips the desktop and opens CYBERDECK across the whole
screen, with no taskbar or window borders.

* **Q** quits to a text prompt. Type `cyberdeck` to start it again,
  `desktop` to open the normal desktop, or `sudo reboot`.
* **Text size:** change `FONT_SIZE` near the top of `launch-cyberdeck.sh`
  (default 12; bigger number = bigger text).
* **SSH** logins are left alone and get a normal shell.
* **Undo:** `bash ~/Documents/UI/uninstall-autostart.sh` boots to the desktop again.

How it works: the Pi boots to the text console and logs in automatically.
Your login profile then starts `launch-cyberdeck.sh`, which runs CYBERDECK
inside *cage* (a one-app full-screen kiosk) and *foot* (a terminal), so you
get full colours and fonts without the desktop. If those two programs aren't
installed, it runs on the plain text console instead, with fewer colours.

The Claude desktop app only runs inside the desktop. While you're in
CYBERDECK mode, type `desktop` first if you want Claude to work with your Pi.

## Fan wiring (Noctua)

| Fan wire        | Pi pin                               |
|-----------------|--------------------------------------|
| Green (tach)    | GPIO2 = physical pin 3               |
| Black (ground)  | any GND, e.g. physical pin 6         |
| Yellow (power)  | 5V (pin 2/4) for 5V fans             |

* GPIO2 has a 1.8k pull-up to 3.3V built into the Pi, so the tach wire can go
  straight to the pin. No resistor needed, and it never sees more than 3.3V.
* GPIO2 is also the I2C data pin. Keep I2C **off**
  (`sudo raspi-config` → Interface Options → I2C → No), or the pin will be busy.
* Noctua fans give 2 pulses per revolution (the default `--ppr 2`).
* A 12V Noctua on a 5V supply will spin slowly or not at all. That's normal.

## Pages

| Key | Page  | What's on it |
|-----|-------|--------------|
| 1   | DASH  | CPU cores, memory/disk, system info, fan, temperature, network, top processes |
| 2   | FAN   | Big RPM readout, spinning rotor, min/avg/max, pulse count, speed gauge, RPM history graph |
| 3   | NET   | Interfaces and IPs, Wi-Fi signal, RX/TX graphs, **D** = gateway/uplink/DNS diagnostics |
| 4   | PROC  | Full process table, **S** switches between CPU and memory sort |
| 5   | TOOLS | Stopwatch with laps, countdown timer, notes (saved to `deck_notes.txt`), password/PIN/UUID generator |
| 6   | TERM  | Run shell commands with **!**, plus an event log of alerts and actions |
| 7   | PTEST | Recon & reference console for your own systems (see below) |
| 8   | SERIAL| Serial terminal for USB devices like a Bus Pirate or Cardputer (see below) |

Global keys: **Tab** or the arrows switch pages, **M** starts the matrix screensaver,
**P** opens the power menu (reboot/shutdown), **?** shows help, **Q** quits.
You can also click or tap the page tabs.

## PTEST — recon & reference (page 7)

A reconnaissance and reference console for **your own** systems. It runs standard
tools you already have installed and inventories them; it contains no attack or
exploit code, and network scans only touch the private subnet this device is on.
Switch sections with **[** and **]**; press **S** to scan/refresh the active one.

* **RECON** — **S** discovers live hosts on your own subnet (uses `nmap` or
  `arp-scan` if installed, otherwise a built-in ping sweep). **F** also scans
  common TCP ports on each host found. Shows IP, MAC, vendor and open ports.
* **WIRELESS** — **S** lists nearby Wi-Fi access points (SSID, signal, channel,
  security) via `nmcli`, and nearby Bluetooth devices via `bluetoothctl`.
  Read-only; it just observes.
* **KIT** — checks which common recon/audit/serial tools are installed, with an
  `apt install` hint for the missing ones. A quick "is my kit ready" view.
  Press **I** to install every missing tool at once; it asks you to press **I**
  again to confirm, then streams apt's output live in the panel.
* **REF** — pentest methodology phases and a physical field-kit packing list.

Scanning tools aren't required — install what you want. The **I** key in the KIT
section installs all the missing ones for you, or run it yourself, e.g.
`sudo apt install nmap arp-scan network-manager bluez`. The RECON ping-sweep
fallback works with no extra tools.

The in-app installer needs passwordless `sudo` (the default for the first user
on Raspberry Pi OS). If it isn't set up, the panel prints the exact
`sudo apt install …` line to paste into a terminal instead.

## SERIAL — USB serial console (page 8)

A dependency-free terminal for talking to a Bus Pirate, M5Stack Cardputer,
Arduino, ESP32, FTDI cable — anything that shows up as USB serial. It auto-lists
`/dev/ttyUSB*`, `/dev/ttyACM*` and the Pi's own UART, and defaults to **115200 8N1**
(the Bus Pirate default; change with `--baud`).

| Key | Does |
|-----|------|
| **[** / **]** | select a port |
| **O** | open (connect to) the selected port |
| **D** | disconnect |
| **Enter** | type a line and send it to the device (Enter sends, Esc leaves) |
| **B** | cycle baud rate (reconnects live) |
| **E** | cycle line ending sent with each line (CRLF / CR / LF / none) |
| **X** | toggle hex / ASCII view of incoming bytes |
| **W** | wipe the scrollback · **R** rescan ports · **↑↓/PgUp/PgDn** scroll |

If a port says *permission denied*, add yourself to the serial group once:
`sudo usermod -aG dialout $USER`, then log out and back in.

A red alert bar flashes if the fan stalls (no pulses for 3 seconds),
the CPU passes 80°C, or the Pi reports under-voltage.

## Useful options

    --max-rpm 5000     fix the gauge's full scale (default: auto)
    --debounce 100     filter electrical noise if RPM reads too high
    --saver 10         matrix screensaver after 10 idle minutes
    --pin 18           tach on a different GPIO
    --no-mouse         keep normal text selection in the terminal

## Troubleshooting

* **FAN page says OFFLINE / GPIO busy**: I2C is probably on (see above).
* **No module named lgpio**: `sudo apt install python3-lgpio`
* **RPM jumps around or reads double**: try `--debounce 100`.
* **Boxes look like question marks**: your terminal font lacks the
  block characters. The default Raspberry Pi OS terminal is fine.
