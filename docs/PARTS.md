# OpenFlight Parts List

Hardware components for building the OpenFlight golf launch monitor.

> **Ordering shortcut:** A shared **[OpenFlight Mouser project](https://www.mouser.com/en/Tools/Project/Share?AccessID=4c97a00bbc)** is available for the parts Mouser stocks — open it, save it to your own Mouser account, and add the whole list to your cart in one step instead of searching for each item. Check it against the tables below before you order: anything Mouser does not carry has a direct vendor link here.

> **Next step after gathering parts:** See the [Raspberry Pi Setup Guide](raspberry-pi-setup.md) for assembly and software installation.

## Core Components

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **OPS243 Radar** | Doppler radar for ball/club speed detection | [OmniPreSense](https://omnipresense.com/product/ops243-doppler-radar-sensor/) | $249 |
| **Raspberry Pi 5** | Main compute unit (4GB+ recommended) | [Adafruit](https://www.adafruit.com/product/5812) | $130 |
| **7" Touchscreen Display** | HMTECH 7" 1024x600 IPS display | [Amazon](https://www.amazon.com/dp/B0D3QB7X4Z) | $46 |

> **NOTE on OPS243-A-W (WiFi version):** The standard **OPS243-A** (USB only) is strongly recommended. The WiFi module on the OPS243-A-W drives the internal UART receive line, preventing direct connection to the Raspberry Pi GPIO UART (Layout A). However, if you already have the WiFi version, it can still be used over USB with a powered USB hub (Layout B) when paired with the IWR6843 angle radar.

> **Display alternative:** The [Raspberry Pi Touch Display 2](https://www.raspberrypi.com/products/touch-display-2/) (7" 720x1280, MIPI DSI) also works with the Pi 5. If you use it, print the `Touch_Display2_backplate.stl` and `Touch_Display2_shell.stl` from the IARC case instead of `monitor_shell.stl` — see the [IARC case instructions](../cad/IARC_case/README.md).

## Sound Trigger (for Rolling Buffer Mode)

The sound trigger detects club impact to precisely time radar captures. Essential for spin detection via rolling buffer mode.

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **SparkFun SEN-14262** | Sound Detector with envelope/gate outputs | [SparkFun](https://www.sparkfun.com/products/14262) | $12 |
| **Through-hole resistor** | For R17 pad on SEN-14262 to reduce sensitivity (see note) | Any electronics supplier | $1 |
| **Jumper wires (female/female, 75 mm)** | 3 wires: GATE → HOST_INT, VCC → 3.3V, GND → GND. Female on both ends — the Pi GPIO header, the OPS243 J3 header, and headers soldered to the SEN-14262 are all male pins. Also covers the OPS243 → Pi ground run. 75 mm is the shortest female/female length Mouser stocks (Adafruit 794, Mouser 485-794, 40-wire strip). $3.95 at Adafruit list; Mouser's price for 485-794 is unverified | [Mouser](https://www.mouser.com/ProductDetail/Adafruit/794) | $4 |

> **R17 resistor:** The SEN-14262 is rated for 5V but runs at 3.3V in this setup, which can cause the GATE output to stick high. Soldering a resistor into the R17 through-hole position (in parallel with the onboard 100kΩ R3) reduces preamp gain and fixes this. Start with 47kΩ; use a lower value (e.g. 33kΩ) if the sensor is still too sensitive for your environment.

### Sound Trigger Wiring

```
SEN-14262               Raspberry Pi           OPS243
┌───────────┐          ┌──────────┐          ┌──────────┐
│ VCC ──────┼──────────┤ 3.3V     │          │          │
│           │          │          │          │          │
│ GATE ─────┼──────────┼──────────┼──────────┤ HOST_INT │
│           │          │          │          │ (J3 P3)  │
│ GND ──────┼──────────┤ GND      ├──────────┤ GND      │
│           │          │          │          │ (J3 P1)  │
└───────────┘          └──────────┘          └──────────┘
```

See [sound-trigger-wiring.md](sound-trigger-wiring.md) for detailed instructions and troubleshooting.

## Angle Radar (TI IWR6843) — CURRENT

This is the supported angle radar. It measures vertical and horizontal launch
angle, and supplies the pre-impact frames club path is derived from.

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **TI IWR6843LEVM** | 60 GHz mmWave evaluation board, 4 RX × 3 TX | [TI](https://www.ti.com/tool/IWR6843LEVM) | $150 |
| **Micro-USB cable (data-capable)** | Connects the LEVM's CP2105 serial bridge to the Pi — the LEVM's USB port is micro-USB. Charge-only cables will not enumerate | Any | $5 |
| **Jumper wire** | 1 wire: detector `GATE` → Pi BCM17 / physical pin 11, alongside the existing `GATE` → OPS `HOST_INT`. Female/female again — comes out of the same 75 mm strip as the sound-trigger wires above | [Mouser](https://www.mouser.com/ProductDetail/Adafruit/794) | $1 |

The board needs **custom firmware** — it does not work out of the box. The
stock TI demo does not expose the raw radar cube OpenFlight needs. A validated
prebuilt image ships in `firmware/releases/`, so you do not need the TI
toolchain to flash it.

You also need physical access to the board's **boot-mode switch (S1.1)** and
**RESET button** to flash. Both are on the LEVM itself; nothing to buy.

### IWR6843 Setup

Two connection layouts are supported, and which one you can use depends on your
OPS243 variant:

| Layout | OPS243 connection | Extra parts needed |
|--------|-------------------|--------------------|
| **A (validated)** | Pi GPIO UART header | 4 jumper wires (5V, GND, TX, RX) |
| **B** | Powered USB hub | [Powered USB hub](https://www.amazon.com/dp/B0CN3F9Y1Z) (~$20) |

Layout A keeps the TI board on USB and moves the OPS243 to the Pi's GPIO
header, which is what the power budget requires — the Pi cannot supply both
radars over USB.

> [!WARNING]
> Layout A does **not** work with a **WiFi-equipped OPS243-A**. Its onboard WiFi
> module already drives the radar's UART receive line, so the Pi cannot send it
> commands. WiFi OPS boards must use Layout B with a powered hub.

Full instructions: **[IWR6843 Operator Guide](iwr6843/README.md)** for wiring,
flashing, mounting, and geometry; **[Moving the OPS243 to the Pi GPIO
UART](ops243-uart-migration.md)** for the OPS side of Layout A.

### Optional Enclosure Inclinometer

An LIS3DH mounted to the enclosure base lets OpenFlight compensate the IWR6843
tilt when the rig is placed on uneven ground.

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **Adafruit LIS3DH breakout** | Triple-axis accelerometer with STEMMA QT connectors | [Adafruit product 2809](https://www.adafruit.com/product/2809) | $5 |
| **JST-SH cable kit (Qwiic-to-Dupont)** | Qwiic/STEMMA QT to female Dupont jumpers, used in the validated build. The LIS3DH plugs into its STEMMA QT socket and the Dupont ends push straight onto the Pi GPIO header, so no soldering is needed — the alternative is soldering a header onto the breakout and wiring that by hand | [Amazon](https://www.amazon.com/Connector-Compatible-Development-Sensors-Drivers/dp/B0GJPRX4YT) | ~$10 |
| **Qwiic-to-Dupont cable (single)** | Mouser-stocked equivalent of the kit above: one JST-SH 4-pin to female Dupont sockets cable (Adafruit 4397, Mouser 485-4397). Enough on its own for the LIS3DH → Pi header run, and it keeps the whole inclinometer orderable from Mouser. 150 mm is the only length Adafruit makes in this JST-SH-to-female-socket configuration; the shorter 50-100 mm Qwiic cables are Qwiic-to-Qwiic and have no Dupont end | [Mouser](https://www.mouser.com/en/ProductDetail/Adafruit/4397) | ~$1 |
| **STEMMA QT / Qwiic-to-Qwiic cable** | Qwiic-to-Qwiic run for mounting the LIS3DH away from the Pi. Sold in 50-400 mm lengths; which one we need is **TODO** — it depends on the enclosure mounting position, and the case is still in development | [Adafruit](https://www.adafruit.com/product/4399) | ~$1 |
| **Qwiic-to-male-headers cable** | Adafruit 4209 (Mouser 485-4209), 150 mm: JST-SH 4-pin on the sensor end, male header pins on the other. Those pins chain into the 75 mm female/female strip from the sound-trigger section, so the LIS3DH run can be lengthened from wires already on hand instead of committing to one fixed Qwiic-to-Qwiic length — the practical option while the case layout is unsettled. $0.95 at Adafruit list; Mouser's price for 485-4209 is unverified | [Mouser](https://www.mouser.com/en/ProductDetail/Adafruit/4209) | ~$1 |

See the **[LIS3DH Inclinometer Setup Guide](inclinometer/README.md)** for wiring,
mounting, calibration, startup flags, and troubleshooting.

---

## Angle Radar (K-LD7) — DEPRECATED

> **⚠️ DEPRECATED — do not buy for new builds.** The K-LD7 angle radars have been superseded by a more capable radar chip. K-LD7 support remains in the software for existing builds but will not receive further development. The parts below are listed for reference only.

Two K-LD7 modules measure launch angle (vertical) and club path / aim direction (horizontal). The OPS243 handles speed; the K-LD7s provide **angle and distance only** (speed data aliases above 62 mph).

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **RFbeam K-LD7 (×2)** | 24 GHz FMCW radar for angle + distance | [RFbeam](https://rfbeam.ch/product/k-ld7-radar-transceiver/) | ~$60 ea |
| **FTDI USB-to-Serial adapter (×2)** | 3.3V FTDI board for K-LD7 UART (e.g. FT232RL) | [Amazon](https://www.amazon.com/s?k=ftdi+3.3v+usb+serial) | ~$10 |

> **EVAL board not required.** The K-LD7 bare module communicates over 3.3V UART (TX, RX, VCC, GND). Any 3.3V FTDI USB-to-serial adapter works. The official K-LD7 EVAL board (~$120 each) is only needed if you want the RFbeam GUI software for configuration — OpenFlight configures the radar over serial automatically.

### K-LD7 Connection

Each K-LD7 connects via a 3.3V FTDI adapter, appearing as `/dev/ttyUSB*` on Linux.

```
K-LD7 Module (UART) → FTDI 3.3V Adapter → USB → Raspberry Pi
```

One unit is mounted vertically (launch angle), one horizontally (club path / aim direction). A `--kld7-angle-offset` parameter corrects for mounting geometry — see the [setup guide](raspberry-pi-setup.md) for calibration.

## Power & Accessories

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **27W USB-C Power Supply** | Official Pi 5 power supply (5V 5A) | [Adafruit](https://www.adafruit.com/product/5814) | $14 |
| **Raspberry Pi Active Cooler** | Clip-on heatsink + fan for the Pi 5 (SC1148). Recommended: the kiosk runs the UI, radar capture, and FFT processing continuously, and a passively cooled Pi 5 throttles under sustained load | [Mouser](https://www.mouser.com/en/ProductDetail/Raspberry-Pi/SC1148) | $8 |
| **Jumper wires (female/male, 75 mm)** | Header-pin extensions: the female end goes onto a Pi GPIO pin and the male end re-presents that pin for a second connector. Used here to keep the 5V rail reachable for the OPS243 when the Touch Display 2 is also wired to the header, instead of one connector covering the whole rail. 75 mm is the shortest female/male length Mouser stocks (Adafruit 1953, Mouser 485-1953, 20-wire ribbon). $1.95 at Adafruit list; Mouser's price for 485-1953 is unverified | [Mouser](https://www.mouser.com/en/ProductDetail/Adafruit/1953) | $2 |
| MicroSD Card (32GB+) | For Pi OS and software | Any Class 10 | $10 |
| USB-A to Micro-USB Cable | For OPS243 radar connection | Any | $5 |

## Optional

| Part | Description | Link | ~Price |
|------|-------------|------|--------|
| **Geekworm X1202 UPS HAT** | Rechargeable Pi 5 power using four matching flat-top 18650 Li-ion cells. Cells are not included | [Geekworm](https://geekworm.com/products/x1202) / [Amazon](https://www.amazon.com/dp/B0CRZ4ZXQW) | ~$48 + cells |
| **Geekworm X1206 UPS HAT** | Larger rechargeable Pi 5 power option using four matching 21700 Li-ion cells, advertised up to 20,000mAh total. Cells are not included | [Geekworm](https://geekworm.com/products/x1206) | Varies + cells |
| **Geekworm 12 mm power button (PSW12)** | Geekworm's pre-wired momentary metal push button for its UPS boards: 12 mm panel hole, IP66, 15 cm lead ending in the XH2.54 2-pin plug that fits the external power-button header on the X1202/X1206, so no soldering or crimping. Geekworm lists it for the X1201/X1202/X1203/X1206. Not found on Amazon or Mouser, so order it from Geekworm, together with the HAT if buying that from Geekworm too | [Geekworm](https://geekworm.com/products/psw12) | $2 |
| **InnoMaker OV9281 global-shutter camera** | High-speed monochrome camera for experimental vision work. Camera software is not enabled in the production kiosk path | [Amazon](https://www.amazon.com/dp/B09WTP5GZH?th=1) | ~$30 |
| **Adafruit DS3502 digital potentiometer** | I2C-controlled 10K digital potentiometer (STEMMA QT / Qwiic). Intended for the SEN-14262 `R17` gain trim: installed in series with a fixed 37kΩ resistor it gives a software-adjustable 37-47kΩ range, so preamp gain can be tuned from code instead of desoldering and swapping a fixed resistor. **Not yet built or tested** — no code drives it, and [sound-trigger-wiring.md](sound-trigger-wiring.md) still assumes a soldered R17 | [Adafruit](https://www.adafruit.com/product/4286) | ~$5 |

See [Camera and YOLO Experiments](yolo-performance-tuning.md) before buying the
camera; the standard setup does not install its optional software dependencies.

---

## Cost Summary

| Category | ~Price |
|----------|--------|
| Core (OPS243, Pi 5, Display) | $355 |
| Sound Trigger (SEN-14262 + resistor + wires) | $17 |
| Power & Accessories | $37 |
| **Subtotal, no angle radar** | **~$409** |
| Angle Radar (IWR6843LEVM + cable + wire) — **current** | $156 |
| **Total with angle radar** | **~$565** |
| Optional Enclosure Inclinometer (LIS3DH + Qwiic cables) | $16 |
| Optional extras (X1202 UPS HAT, four 18650 cells, 12 mm power button, OV9281 camera) | $104 |
| **Complete build (total with angle radar + inclinometer + optional extras)** | **~$685** |
| Angle Radar (2× K-LD7 + FTDI adapters) — **deprecated** | $140 |

The complete-build line uses the X1202 as the UPS (not the X1206), estimates its
four flat-top 18650 cells at ~$6 each ($24; a Samsung 35E, Molicel P28A, or LG
MJ1 each sells for about that), and leaves out the deprecated K-LD7 path and the
untested DS3502.

OpenFlight works without any angle radar: you get ball speed, club speed, smash
factor, spin rate, and estimated carry. The angle radar adds measured launch
angle (vertical and horizontal) and is what club path is derived from.

If you are building new, buy the **IWR6843**, not the K-LD7s. It costs about the
same as the two K-LD7s plus their FTDI adapters ($156 vs $140) and replaces both
of them with one board. The K-LD7 path is **deprecated** and kept only so
existing builds keep working.
