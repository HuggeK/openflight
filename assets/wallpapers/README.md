# Wallpapers

Desktop wallpapers sized for the Raspberry Pi Touch Display 2 (7" or 5") used in
landscape in the IARC monitor shell (`cad/IARC_case/stl/monitor_shell_rpi_touch_v2.stl`).

| File | Size | Notes |
|------|------|-------|
| `openflight-sunset-1280x720.jpg` | 1280×720 | Sunset over a fairway, photo by @HuggeK |

The panel is natively **720×1280 portrait**, so the desktop has to be rotated
before a 1280×720 wallpaper fits.

## 1. Rotate the desktop to landscape

**Desktop (persists across reboots):** Control Centre → Screens → right-click
`DSI-1` → Orientation → **Left** (90° clockwise) → Apply → OK.
If the picture ends up upside down for your mounting, pick **Right** instead.

**From SSH (to test first, not persistent):**

```bash
WAYLAND_DISPLAY=wayland-0 wlr-randr --output DSI-1 --transform 90
```

Touch follows the rotation on the desktop. To also rotate the boot/text console,
append to the single line in `/boot/firmware/cmdline.txt`:

```
video=DSI-1:720x1280@60,rotate=90
```

## 2. Set the wallpaper

Right-click the desktop → Desktop Preferences → Picture → pick the file, layout
"Crop" (or "Fit"). From a terminal in the desktop session:

```bash
pcmanfm --set-wallpaper ~/openflight/assets/wallpapers/openflight-sunset-1280x720.jpg --wallpaper-mode=crop
```

The wallpaper is only visible outside kiosk mode; `scripts/start-kiosk.sh`
covers the screen with Chromium.
