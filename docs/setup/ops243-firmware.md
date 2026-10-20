---
icon: lucide/cpu
---

# Update OPS243 Firmware

**Only needed if your OPS243-A reports firmware older than 1.3.2** and you want
the [internal hardware trigger](../hardware-trigger.md). Send `?V` in a serial
terminal to check.

OpenFlight ships a flasher that drives a SEGGER J-Link directly, so you can
update the radar **from the Pi** with the probe plugged into one of its USB
ports. It replaces Infineon's XMC Flasher: no Windows PC, no Java/JavaFX, no
Infineon tools. The same script also runs on any Linux, macOS or Windows
machine that has the J-Link software installed.

!!! warning "New tool: probe first"

    The flashing tool is covered by unit tests against recorded J-Link
    Commander behaviour, but has not yet been run against every OPS243 board
    revision. Run `--probe` first; it never writes anything.

## What you need

| Item | Notes |
| --- | --- |
| SEGGER J-Link | The **J-Link EDU Mini** listed in [Parts](../get-started/parts.md) is enough. Its EDU licence covers non-commercial use only. |
| 1.27 mm Cortex debug cable | Comes with the EDU Mini. Plugs into the radar's keyed **`J2`** header, not the `J3` header OpenFlight wires to. |
| Firmware `.hex` | Not a public download. Ask OmniPreSense on the project Discord, via the [contact page](https://omnipresense.com/contact/), or at customerservice@omnipresense.com. |
| Power for the radar | The EDU Mini does **not** power the target. Keep the OPS243 on USB or J3 5 V while flashing. |

## 1. Install the J-Link software on the Pi

1. On [segger.com/downloads/jlink](https://www.segger.com/downloads/jlink/),
   download **J-Link Software and Documentation Pack → Linux, ARM 64-bit,
   DEB installer** and accept SEGGER's licence.
2. Copy it to the Pi and install it:

    ```bash
    sudo apt install ./JLink_Linux_V*_arm64.deb
    ```

3. **Unplug and replug the J-Link** so the udev rule the package installs
   takes effect, then confirm the probe is visible:

    ```bash
    JLinkExe
    J-Link> ShowEmuList
    J-Link> exit
    ```

    You should see a line with `Serial number:` and `J-Link EDU Mini`. An
    empty list means the probe is not visible yet.

## 2. Connect the radar

1. Stop OpenFlight and anything else using the radar.
2. Plug the debug cable into `J2`. It is keyed; do not force it.
3. Keep the radar powered over USB (or J3 5 V).

## 3. Probe (read-only)

```bash
uv run python firmware/flash_ops243.py --probe
```

This finds the J-Link, connects over SWD and reads the chip ID register to
identify the MCU. Expected:

```text
J-Link:  /usr/bin/JLinkExe
Probe:   J-Link EDU Mini #801012345
Chip:    SCU_IDCHIP @0x50004004 = 0x00045002 -> XMC4500
Device:  XMC4500-1024 (1024 KiB flash)

Probe OK: J-Link and MCU are reachable. Nothing was written.
```

The exact `SCU_IDCHIP` value depends on the silicon step. A board that
reports an XMC4700 is picked up automatically as `XMC4700-2048`.

## 4. Flash

```bash
uv run python firmware/flash_ops243.py ~/OPS243-A_v1.3.2.hex
```

Builds with the OPS243 on the GPIO UART should add `--port /dev/ttyAMA0` so
the tool can read `?V` before and after (USB builds are found automatically).
Type `FLASH` when asked. The tool:

1. checks the HEX file (record checksums, end-of-file record) and that it fits
   the detected MCU's flash;
2. programs it with J-Link's `loadfile`, which only rewrites the flash sectors
   the image covers. **It never runs a full-chip erase**, so it does not do
   what XMC Flasher's *Erase* button does;
3. reads every programmed range back and compares it byte for byte;
4. resets the radar and reads the new version over serial:

```text
Programming and verifying...
Flash complete and verified by read-back.
OPS243:  firmware 1.3.1 -> 1.3.2
```

Then unplug the debug cable, power-cycle the radar, and run the
[rolling buffer check](rolling-buffer.md):

```bash
uv run python scripts/hardware-test/test_rolling_buffer_persist.py --test
```

If it fails, the update reset the saved settings; rerun `--setup` as that page
describes.

## Options

| Option | Use |
| --- | --- |
| `--probe` | Check the J-Link and identify the MCU; never writes. |
| `--device XMC4500-1024` | Force the J-Link device instead of detecting it. A choice that contradicts the chip is always refused. |
| `--skip-chip-check` | With `--device`, flash even if the chip ID cannot be read. |
| `--interface JTAG` | Use JTAG instead of SWD. |
| `--speed 1000` | Lower the debug clock (kHz) for long or flaky cables. |
| `--usb SERIAL` | Choose a probe when several J-Links are attached. |
| `--port /dev/ttyAMA0` | Serial port for the before/after `?V` check. |
| `--no-version-check` | Do not open the radar's serial port at all. |
| `--no-verify` | Skip the read-back comparison (not recommended). |
| `--jlink PATH` | J-Link Commander location if it is not on `PATH`. |
| `--yes` | Skip the `FLASH` confirmation. |

## Troubleshooting

**`No J-Link found on USB`.** Replug the probe after installing the package.
On Windows, rerun the J-Link installer as administrator and tick *Install
legacy USB Driver for J-Link*.

**`Could not talk to the MCU`.** The radar is not powered, or the cable is in
the wrong header. Check `J2`, then retry with `--speed 1000`, then with
`--interface JTAG`.

**`Could not identify the MCU from SCU_IDCHIP`.** Send `?P` to confirm the
part, then rerun with `--device` and `--skip-chip-check`.

**`Verify FAILED`.** Leave everything connected and run the same command
again. The radar keeps running the old firmware until a flash completes.

## Other computers

The script is the same everywhere; only the J-Link package differs. On
Windows it finds `JLink.exe` under `C:\Program Files\SEGGER`, and on macOS
under `/Applications/SEGGER`, so no `--jlink` is needed for a default install.
