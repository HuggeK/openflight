#!/usr/bin/env python3
"""Flash OPS243 firmware (Intel HEX) through a SEGGER J-Link, e.g. from the Pi.

Replaces Infineon's XMC Flasher for OpenFlight builders. Works anywhere the
SEGGER J-Link Software Pack is installed: Raspberry Pi OS (arm64), other
Linux, macOS and Windows. See docs/setup/ops243-firmware.md.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from openflight.ops243_jlink import (
    SCU_IDCHIP_ADDRESS,
    XMC_DEVICES,
    FlashError,
    JLinkCommander,
    Probe,
    family_from_idchip,
    find_jlink_exe,
    parse_intel_hex,
    resolve_device,
    validate_segments_for_device,
)

# USB CDC needs a moment to re-enumerate after the MCU restarts.
POST_FLASH_SETTLE_S = 3.0
POST_FLASH_VERSION_ATTEMPTS = 5


def read_ops_version(port: Optional[str]) -> Optional[str]:
    """Best-effort ``?V`` over serial; never raises.

    Uses the regular OpenFlight driver so baud negotiation and dump draining
    behave exactly as they do at runtime.
    """
    # Imported lazily: the flash path itself must not depend on serial access.
    from openflight.ops243 import OPS243Radar  # pylint: disable=import-outside-toplevel

    radar = OPS243Radar(port=port)
    try:
        radar.connect()
        return radar.get_firmware_version()
    except Exception:  # pylint: disable=broad-except
        return None
    finally:
        try:
            radar.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def choose_probe(probes: list[Probe], requested: Optional[str]) -> Probe:
    """Pick the probe to use, refusing to guess between several."""
    if not probes:
        raise FlashError(
            "No J-Link found on USB. Check the cable, and on Linux replug the probe "
            "after installing the J-Link package so its udev rule applies. "
            "On Windows, install the J-Link legacy USB driver (see the docs)."
        )
    if requested:
        for probe in probes:
            if probe.serial == requested:
                return probe
        raise FlashError(f"J-Link {requested} is not connected; found {_describe(probes)}.")
    if len(probes) > 1:
        raise FlashError(
            f"Several J-Links are connected ({_describe(probes)}); pick one with --usb."
        )
    return probes[0]


def _describe(probes: list[Probe]) -> str:
    return ", ".join(f"{p.product} #{p.serial}" for p in probes)


def confirm(prompt: str, expected: str, ask: Callable[[str], str] = input) -> bool:
    """Require the user to type an exact word before a destructive step."""
    return ask(prompt).strip() == expected


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Flash OPS243 firmware through a SEGGER J-Link (replaces XMC Flasher)."
    )
    parser.add_argument(
        "image", type=Path, nargs="?", help="OPS243 firmware .hex from OmniPreSense"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="only check the J-Link and identify the MCU; never erases or writes",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", *XMC_DEVICES],
        help="J-Link device name (default: detect the family from the chip)",
    )
    parser.add_argument(
        "--skip-chip-check",
        action="store_true",
        help="flash an explicit --device even if the chip ID cannot be read",
    )
    parser.add_argument("--interface", default="SWD", choices=["SWD", "JTAG"])
    parser.add_argument("--speed", type=int, default=4000, help="debug clock in kHz (default 4000)")
    parser.add_argument(
        "--usb", metavar="SERIAL", help="J-Link serial number when several are attached"
    )
    parser.add_argument("--jlink", metavar="PATH", help="path to JLinkExe / JLink.exe")
    parser.add_argument(
        "--port",
        help="OPS243 serial port for reading ?V before and after (default: auto-detect USB)",
    )
    parser.add_argument(
        "--no-version-check",
        action="store_true",
        help="do not open the OPS243 serial port before/after flashing",
    )
    parser.add_argument("--no-verify", action="store_true", help="skip the byte-for-byte read-back")
    parser.add_argument(
        "--yes", action="store_true", help="skip the interactive FLASH confirmation"
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    commander_factory: Callable[..., JLinkCommander] = JLinkCommander,
    version_reader: Callable[[Optional[str]], Optional[str]] = read_ops_version,
    ask: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the guarded flashing workflow. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.probe and args.image is None:
        parser.error("image is required unless --probe is used")

    segments = []
    if not args.probe:
        try:
            text = args.image.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as error:
            parser.error(f"cannot read {args.image}: {error}")
        try:
            segments = parse_intel_hex(text)
        except FlashError as error:
            parser.error(f"{args.image} is not a valid Intel HEX file: {error}")
        total = sum(len(s.data) for s in segments)
        print(f"Image:   {args.image.resolve()}")
        print(f"Size:    {total:,} bytes in {len(segments)} range(s)")
        for segment in segments:
            print(f"         0x{segment.address:08X}-0x{segment.end - 1:08X}")
        print(f"SHA256:  {hashlib.sha256(args.image.read_bytes()).hexdigest()}")

    workdir = Path(tempfile.mkdtemp(prefix="ops243-flash-"))
    try:
        return _run(args, segments, workdir, commander_factory, version_reader, ask, sleep)
    except FlashError as error:
        print(f"\nError: {error}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run(args, segments, workdir, commander_factory, version_reader, ask, sleep) -> int:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    exe = find_jlink_exe(args.jlink)
    print(f"J-Link:  {exe}")

    discovery = commander_factory(exe, workdir)
    probe = choose_probe(discovery.list_probes(), args.usb)
    print(f"Probe:   {probe.product} #{probe.serial}")

    jlink = commander_factory(
        exe, workdir, interface=args.interface, speed_khz=args.speed, usb_serial=probe.serial
    )
    try:
        idchip = jlink.read_idchip()
    except FlashError as error:
        raise FlashError(
            f"{error}\n\nCould not talk to the MCU. Is the OPS243 powered (USB or J3 "
            "5 V)? The J-Link EDU mini does not power the target. Check the "
            "debug cable orientation, or try --interface JTAG / --speed 1000."
        ) from error
    family = family_from_idchip(idchip) if idchip is not None else None
    shown = f"0x{idchip:08X}" if idchip is not None else "unreadable"
    print(f"Chip:    SCU_IDCHIP @0x{SCU_IDCHIP_ADDRESS:08X} = {shown} -> {family or 'unknown'}")

    device = resolve_device(args.device, family, skip_chip_check=args.skip_chip_check)
    print(f"Device:  {device.jlink_name} ({device.flash_bytes // 1024} KiB flash)")

    if args.probe:
        print("\nProbe OK: J-Link and MCU are reachable. Nothing was written.")
        return 0

    validate_segments_for_device(segments, device)

    before = None
    if not args.no_version_check:
        before = version_reader(args.port)
        print(f"OPS243:  firmware {before or 'not readable over serial (continuing)'}")

    if not args.yes:
        print("\nThis reprograms the OPS243 flash sectors the image covers (no full-chip erase).")
        print("Stop OpenFlight first, and keep the radar powered until this finishes.")
        if not confirm("\nType FLASH to begin: ", "FLASH", ask):
            print("Flash cancelled.")
            return 2

    print("\nProgramming" + ("" if args.no_verify else " and verifying") + "...", flush=True)
    # Stage the HEX in the whitespace-free workdir: J-Link Commander's script
    # parser cannot take paths with spaces (common on Windows/macOS).
    staged = workdir / "image.hex"
    shutil.copyfile(args.image, staged)
    jlink.flash(device, staged, segments, verify=not args.no_verify)
    print(
        "Flash complete" + (" (unverified)." if args.no_verify else " and verified by read-back.")
    )

    if not args.no_version_check:
        sleep(POST_FLASH_SETTLE_S)
        after = None
        for _ in range(POST_FLASH_VERSION_ATTEMPTS):
            after = version_reader(args.port)
            if after:
                break
            sleep(1.0)
        if after:
            print(f"OPS243:  firmware {before or '?'} -> {after}")
        else:
            print("OPS243:  could not read ?V yet; power-cycle the radar and check with ?V.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
