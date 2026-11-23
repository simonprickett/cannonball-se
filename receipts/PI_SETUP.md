# Setting up the receipt printer on a Raspberry Pi

Written from a bring-up on a Pi Model A+ (Raspberry Pi OS Lite 32-bit, Debian 13, Python 3.13) with a CSN-A2
58 mm thermal printer. **Everything here worked on the A+. The Pi 4 has not been tried yet**; the places where
a Pi 4 differs are marked *Pi 4*. Assume a fresh Raspberry Pi OS install with SSH working.

What you get at the end: the CLI prints the fixture receipts (text, logo, QR) on the printer. Hooking it to real
games (Alloy, MQTT bridge, subscriber) is separate; see `../LOG_REPLAY_PLAN.md`.

## 1. Printer wiring

Power everything off before touching the GPIO header.

| Pi | Printer |
|---|---|
| Pin 8 (GPIO14, TX) | RXD |
| Pin 6 (GND) | GND |
| Pin 10 (GPIO15, RX) | **not connected** |

- **Leave the printer's TX unconnected.** The Pi's GPIO is 3.3 V only and not 5 V tolerant, and we could not confirm
  this printer's TX level. Printing is one-way, so nothing is lost. (On the A+ the TX wire was cut and insulated.)
- Power the printer from **its own 5 V 2 A supply**, not from the Pi. Load paper.
- Confirm the printer is the **TTL** variant (not RS232). Hold the feed button while powering on to print the
  self-test. Ours reported 9600 baud 8N1, 48 bytes per row (384 dots), density 4 of 8, code page CP437.

## 2. Free the UART (enable it, remove the console)

```
sudo usermod -aG dialout $USER
sudo raspi-config
```

Interface Options, Serial Port: answer **No** to "login shell over serial", **Yes** to "serial port hardware
enabled". Reboot, log back in, then check:

```
ls -l /dev/serial0          # a symlink to ttyAMA0 (or ttyS0, see below)
cat /proc/cmdline           # must NOT contain console=serial0, console=ttyAMA0, console=ttyS0 or kgdboc
id                          # must include dialout
systemctl is-enabled serial-getty@ttyAMA0.service   # disabled (also check serial-getty@ttyS0)
```

**If `/proc/cmdline` still has a serial console**, look at the file it comes from:
```
cat /boot/firmware/cmdline.txt
```
On the A+ this file was **empty**, so the firmware used its built-in default (which includes the serial console) and
`raspi-config` changes had no effect. If yours is empty, write a one-line file with your real root device (check with
`findmnt /`):
```
echo 'console=tty1 root=/dev/mmcblk0p2 rootfstype=ext4 fsck.repair=yes rootwait' | sudo tee /boot/firmware/cmdline.txt
sudo reboot
```
If it is not empty, remove `console=serial0,115200` (and any `kgdboc=...`) with a single-line edit and keep a backup.

**Pi 4:** Bluetooth owns the full UART (`ttyAMA0`) on a Pi 4, so `/dev/serial0` normally points at the **mini UART**
(`ttyS0`), whose baud rate depends on the core clock and can be unreliable. Try 9600 as is. If the printer prints
garbage, add one of these to `/boot/firmware/config.txt` and reboot:
- `dtoverlay=disable-bt` (also `sudo systemctl disable hciuart`): frees the full UART for `serial0`, but loses Bluetooth.
- `dtoverlay=miniuart-bt`: gives the full UART to `serial0` and moves Bluetooth to the mini UART.
If the game uses a Bluetooth controller, prefer `miniuart-bt`. (Untested on the Pi 4.)

## 3. Get the code and install

```
git clone --depth 1 --branch reproducible-data-set <repo-url> ~/cannonball-se     # or rsync receipts/ over
cd ~/cannonball-se/receipts
sudo apt install -y python3-venv python3-yaml python3-pil python3-serial
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

The `apt` packages and `--system-site-packages` stop `pip` trying to compile PyYAML and Pillow, which takes very
long on small Pis (it was the problem on the A+). On a Pi 4 with 64-bit OS pre-built wheels usually exist, so a plain
`python3 -m venv .venv` is probably fine, but the above is safe.

Dry run, no printer needed:
```
.venv/bin/python print_receipt.py fixtures/high_score.json --sink file:out/x.raw      # wrote 421 bytes
```

## 4. First prints

Cheapest first, so a wiring problem costs little paper:

```
.venv/bin/python print_receipt.py fixtures/high_score.json --sink serial:/dev/serial0                  # text only
.venv/bin/python print_receipt.py fixtures/completed.json --sink serial:/dev/serial0 --logo assets/logo.png
export RECEIPT_LOGO_PATH=assets/logo.png RECEIPT_QR_ENABLED=true RECEIPT_DASHBOARD_BASE_URL=https://play.example.org
.venv/bin/python print_receipt.py fixtures/completed.json --sink serial:/dev/serial0                   # logo + QR
```

Settings (logo, QR mode and size, feed lines) are in the README's configuration table and `receipts.env.example`.

Nothing should print while the Pi boots. If something does, the serial console is still attached (section 2).

## 5. Things that are specific to this printer

- **Rows print about 1.2 times taller than columns are wide.** A 3x3-dot QR module came out 20 x 24 mm, so the
  bitmap QR defaults to 4 wide x 3 tall (about 26 x 24 mm). The logo looked fine uncorrected.
- **Native QR (`GS ( k`) works but only for short data**: 29 characters printed, 147 did not. The cutoff is untested.
  `RECEIPT_QR_MODE=auto` (default) uses native up to `RECEIPT_QR_NATIVE_MAX_CHARS` (29) and the bitmap above that.
  Bitmap QR takes about 8 seconds at 9600 baud.
- **No cutter:** `RECEIPT_FEED_LINES=8` feeds the receipt clear of the tear bar.
- **Serial settings:** 9600 8N1, and `dsrdtr=False` (set in `print_receipt.py`), because only TX and GND are wired.
- **Timezone:** the receipt prints the Pi's local time and a fresh Pi is on UTC. Set it with
  `sudo timedatectl set-timezone <Area/City>`.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ValueError: bad marshal data` | A corrupted `__pycache__` (happened once after an unclean stop). `rm -rf __pycache__`, and shut the Pi down properly. |
| Nothing prints | Wiring (pin 8 to RXD, shared ground), printer supply, paper. Run the printer self-test to isolate. |
| Garbage characters | Baud rate (check the self-test), or on a Pi 4 the mini UART clock (section 2). |
| Text from the Pi's boot on paper | Serial console still enabled (section 2). |
| Receipt prints but no QR | Link too long for native QR and mode forced to `native`; use `auto` or `bitmap`. |
| `pip install` compiles for ages | Use the `apt` packages and `--system-site-packages` (section 3). |
| SSH refused on a fresh image | Enable SSH in Raspberry Pi Imager's settings, or put an empty `ssh` file on the boot partition. |
