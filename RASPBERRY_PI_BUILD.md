# Raspberry Pi 4 Build Instructions

These instructions are for setting up and building on a Raspberry Pi 4.

**Tested on:** Raspberry Pi 4 (8GB RAM), Raspberry Pi OS Lite, release 15 Sep 2026, 64-bit, kernel 6.18, Debian 13 (trixie), 32GB micro SD card (overkill — 8GB should suffice).

## Shopping List

- Raspberry Pi 4 (4GB or 8GB RAM)
- Micro SD card (8GB or larger, use a good brand high quality card)
- Raspberry Pi 4 power supply (get the proper one, underpowered Pis behave erratically, trash SD cards and are frustrating to debug and use)
- USB keyboard
- HDMI display (optional: sound)
- Appropriate cable to connect the Pi 4's Micro HDMI to the HDMI display
- Any required adapter to write the Micro SD card from your laptop/desktop (e.g. a USB Micro SD card reader or a Micro SD to regular SD card adapter)
- HDMI to USB converter (optional). This can be used with software such as "Camera Window" for macOS to bring the Pi's display, and hence the game, into the same screen that's showing the Grafana dashboard.

## Flash the OS

- Download and install the Raspberry Pi Imager from https://www.raspberrypi.com/software/
- Start the Raspberry Pi Imager application and select **Raspberry Pi 4** from the device options.
- Click **Next**, then choose **Raspberry Pi OS (other)** from the list of operating systems. Choose **Raspberry Pi OS Lite (64-bit)** from the second list of operating systems and click **Next**.
- Connect the micro SD card for the Pi (8GB or larger) to the machine running the Pi Imager and select the correct **USB Mass Storage Device Media** item when prompted by the Pi Imager. Then click **Next**.
- When asked for a hostname, name the machine something memorable (e.g. `cannonball`) — write this down — and press **Next**.
- When asked for localisation settings, select your capital city time zone and keyboard layout, then press **Next**.
- You'll now be asked for a username and password. Write these down, confirm the password, and click **Next**.
- Next you're asked for WiFi details. Enter your SSID and password, confirm the password, and click **Next**.
- Click the toggle to enable SSH and choose your preferred authentication mechanism. If you don't want to create certificates, choose the password option. Click **Next**.
- Optionally, enable Raspberry Pi Connect if you have this set up and want to use it. Leave it disabled otherwise. Click **Next**.
- Click **Write**, then confirm **"I understand, erase and write"**.
- Wait for the writing process to complete. If using a laptop, it's a good idea to connect the power adapter.
- Once writing is complete, the Imager will perform a verification pass. Do not skip this.
- Once you see the **"Write complete!"** message, click **Finish** and remove the micro SD card from the computer.

## Raspberry Pi OS Setup

- Insert the micro SD card into the Pi 4, attach a keyboard and a HDMI display, then boot the Pi by connecting the power adapter to it. Be sure to use an appropriate power adapter for the Pi 4 in order to get enough power to it. Underpowering the device can cause random reboots and corrupt your micro SD card.
- Once it boots, log in with the username and password you set in the Imager software.
- Check you have network connectivity by pinging Google:

  ```
  ping google.com
  ```

  If you see response times, press Ctrl-C to exit. If you get timeouts, your WiFi settings need checking.

- Next check SSH is working. From another host on the same WiFi:

  ```
  ssh <username>@<hostname>.local
  ```

  Log in with your password and make sure you get to a shell prompt.

### raspi-config

Run raspi-config to check/configure system properties:

```
sudo raspi-config
```

- Choose option 8 to update the tool. It will restart afterwards.
- Choose option 1, System Options.
- Choose option S2 Audio, and set the audio output to `1 vc4-hdmi-0`.
- Choose option 2, Display Options.
- Choose option D2 Screen Blanking and select No to disable it.
- Choose option 6, Advanced Options. Select A1 Expand Filesystem. Acknowledge the message that says this will happen on the next reboot.
- Select Finish to exit raspi-config and choose Yes to reboot now.
- Allow the system to reboot and log back in again (console or SSH) before continuing.

### Installing Git Tools

- Install the git command line tools:

  ```
  sudo apt install git
  ```

- Once this completes, verify that you have a git command:

  ```
  git --version
  ```

### Get the Project Source Code

- Get the project source code from GitHub:

  ```
  git clone https://github.com/simonprickett/cannonball-se.git
  ```

- Next change directory into the project folder:

  ```
  cd cannonball-se
  ```

### Run the Project Install Script

Run the project install script:

```
./install.sh
```

This script installs dependencies and build tools, updates the system, installs and compiles the OpenTelemetry SDK, and builds the Cannonball SE binary.

This may take 10 minutes or longer, so be prepared for that and have patience!

- Enter your password when asked — this is so that the script can sudo actions for you.
- You'll see lots of compiler warnings while Cannonball SE itself is compiling — don't worry about these.
- If asked to choose from available audio devices, select the option corresponding to `vc4-hdmi-0`. This will likely be option 1.
- When asked if you want to view the man page, enter N to skip it.

`install.sh` should now have completed successfully. Verify you have a binary:

```
ls -l build/cannonball-se
```

The next step is to reboot the Pi:

```
sudo reboot
```

Once it has rebooted, log back in again.

### Configuring Cannonball

- Change back to the cannonball-se folder:

  ```
  cd cannonball-se
  ```

- Edit `config.xml`:

  ```
  vi config.xml
  ```

- Use the information in the main project `README.md` to configure the Grafana Cloud OTel endpoint and other settings.
- When you're done save your changes.

### Install the ROM Files

- Change to the ROMs folder:

  ```
  cd ~/cannonball-se/roms
  ```

- Copy the required ROM files (names listed in `roms.txt`) into this folder.

### Run and Test the Game

- First, change to the project root:

  ```
  cd ~/cannonball-se
  ```

- Start the game. Note that graphical output will appear on the HDMI device and keyboard input only works from the keyboard connected to the Pi. While you can start the game from an SSH session and use that to monitor stdout/stderr, you can't play it like this.

  ```
  build/cannonball-se
  ```
