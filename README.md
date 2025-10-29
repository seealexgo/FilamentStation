# Filament Station v1.1 (Quick-Pair Moves)

A lightweight, Pi-friendly filament management kiosk:

- ✅ Scan QR codes on spools (webcam)
- ✅ **Quick-pair moves:** scan spool + location QR within 10s to auto-move
- ✅ Store spool records locally (SQLite)
- ✅ Update weight & location quickly from a 3.5" TFT touchscreen
- ✅ Open 3DFilamentProfiles (or your short URL) for detailed edits
- 🚀 Ready for upgrades (ESP32 humidity sensors, load cells, Spoolman sync)

> Designed for Raspberry Pi Zero 2 W + Inland 3.5" TFT + cheap USB webcam.


## Quick Start

1) **System deps** (for QR scanning & Tk UI):
```bash
sudo apt-get update
sudo apt-get install -y libzbar0 python3-tk

# If OpenCV wheel is too heavy on your Pi:
#   sudo apt-get install -y python3-opencv

This repo is for development of FilamentStation. The intent is for this to be a 3D printer filament management station based around Pi hardware and the Bambu Lab A1/AMS Lite. Check back for updates.