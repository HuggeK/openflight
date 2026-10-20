"""Flash OPS243 firmware through a SEGGER J-Link from Linux, macOS or Windows.

The OPS243 runs on an Infineon XMC4500 (some board revisions may carry an
XMC4700). OmniPreSense distributes firmware as Intel HEX and documents
flashing with Infineon's XMC Flasher, a JavaFX GUI that is awkward to start on
current JDKs and is not available for the Pi. XMC Flasher is itself a front
end to SEGGER's J-Link library, so this module drives SEGGER's J-Link
Commander (``JLinkExe``) directly instead:

1. ``ShowEmuList`` confirms exactly which probe will be used.
2. ``mem32`` reads the XMC4 ``SCU_IDCHIP`` register so the device family is
   detected from the silicon rather than guessed.
3. ``loadfile`` programs the HEX image. J-Link erases only the sectors the
   image touches; this module never issues ``erase`` (a full-chip erase), so
   flash outside the image is left alone, unlike XMC Flasher's Erase button.
4. ``savebin`` reads every programmed range back so the image is verified
   byte-for-byte on the host, independent of J-Link's own checks.

Everything that talks to the probe goes through :class:`JLinkCommander`, which
takes an injectable ``run`` callable so tests never need hardware.
"""

from __future__ import annotations

import glob
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Device catalogue
# ---------------------------------------------------------------------------

# XMC4 program flash is mapped twice: a cached alias and an uncached alias.
# Vendor HEX files use either one, so both are accepted.
FLASH_CACHED_BASE = 0x0800_0000
FLASH_UNCACHED_BASE = 0x0C00_0000

# SCU_GENERAL.IDCHIP on every XMC4 part. Bits [19:12] carry the family
# number (0x45 for XMC4500, 0x47 for XMC4700), e.g. 0x0004_5002.
SCU_IDCHIP_ADDRESS = 0x5000_4004


@dataclass(frozen=True)
class XMCDevice:
    """One J-Link device name and the flash it exposes."""

    jlink_name: str
    family: str
    flash_bytes: int


XMC_DEVICES: Dict[str, XMCDevice] = {
    device.jlink_name: device
    for device in (
        XMCDevice("XMC4500-1024", "XMC4500", 1024 * 1024),
        XMCDevice("XMC4500-768", "XMC4500", 768 * 1024),
        XMCDevice("XMC4500-512", "XMC4500", 512 * 1024),
        XMCDevice("XMC4700-2048", "XMC4700", 2048 * 1024),
        XMCDevice("XMC4700-1536", "XMC4700", 1536 * 1024),
    )
}

# Used when ``--device auto`` detects a family. The OPS243 boards seen so far
# carry the largest flash option of their family; a smaller part can always
# be selected explicitly.
DEFAULT_DEVICE_FOR_FAMILY: Dict[str, str] = {
    "XMC4500": "XMC4500-1024",
    "XMC4700": "XMC4700-2048",
}

_IDCHIP_FAMILIES: Dict[int, str] = {
    0x44: "XMC4400",
    0x45: "XMC4500",
    0x47: "XMC4700",
    0x48: "XMC4800",
}


class FlashError(RuntimeError):
    """Raised for any condition that must stop a flash before or after it runs."""


def family_from_idchip(idchip: int) -> Optional[str]:
    """Return the XMC4 family named by an ``SCU_IDCHIP`` value, if known."""
    if (idchip >> 16) & 0xFFFF != 0x0004:
        return None
    return _IDCHIP_FAMILIES.get((idchip >> 12) & 0xFF)


def resolve_device(
    requested: str, detected_family: Optional[str], *, skip_chip_check: bool
) -> XMCDevice:
    """Pick the J-Link device, refusing a selection that contradicts the chip.

    Args:
        requested: ``"auto"`` or a key of :data:`XMC_DEVICES`.
        detected_family: Family read from ``SCU_IDCHIP``, or None if unknown.
        skip_chip_check: Accept an explicit device even when the family could
            not be confirmed. Never overrides a *contradicting* detection.
    """
    if requested == "auto":
        if detected_family is None:
            raise FlashError(
                "Could not identify the MCU from SCU_IDCHIP. Pass --device "
                "explicitly together with --skip-chip-check if you are sure "
                "which chip is on the board."
            )
        name = DEFAULT_DEVICE_FOR_FAMILY.get(detected_family)
        if name is None:
            raise FlashError(
                f"Detected {detected_family}, which is not an OPS243 MCU this tool "
                "knows how to flash."
            )
        return XMC_DEVICES[name]

    device = XMC_DEVICES.get(requested)
    if device is None:
        raise FlashError(f"Unknown device {requested!r}; choose one of {', '.join(XMC_DEVICES)}.")
    if detected_family is None:
        if not skip_chip_check:
            raise FlashError(
                "Could not confirm the MCU family from SCU_IDCHIP. Re-run with "
                "--skip-chip-check to flash anyway as " + device.jlink_name + "."
            )
    elif detected_family != device.family:
        raise FlashError(
            f"--device {device.jlink_name} is an {device.family}, but the board "
            f"reports an {detected_family}. Refusing to flash."
        )
    return device


# ---------------------------------------------------------------------------
# Intel HEX
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A contiguous run of bytes at an absolute address."""

    address: int
    data: bytes

    @property
    def end(self) -> int:
        """First address past the segment."""
        return self.address + len(self.data)


def parse_intel_hex(text: str) -> List[Segment]:  # pylint: disable=too-many-branches
    """Parse Intel HEX into sorted, merged, non-overlapping segments.

    Supports data (00), EOF (01), extended segment address (02), start
    segment address (03), extended linear address (04) and start linear
    address (05) records. Every record checksum is verified and the file must
    end with exactly one EOF record.
    """
    chunks: Dict[int, int] = {}
    upper = 0
    seen_eof = False

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if seen_eof:
            raise FlashError(f"HEX line {line_no}: data after the end-of-file record")
        if not line.startswith(":"):
            raise FlashError(f"HEX line {line_no}: record does not start with ':'")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as error:
            raise FlashError(f"HEX line {line_no}: invalid hex digits") from error
        if len(record) < 5:
            raise FlashError(f"HEX line {line_no}: record too short")
        count, addr_hi, addr_lo, rtype = record[0], record[1], record[2], record[3]
        if len(record) != count + 5:
            raise FlashError(f"HEX line {line_no}: byte count does not match record length")
        if sum(record) & 0xFF:
            raise FlashError(f"HEX line {line_no}: checksum mismatch")
        payload = record[4 : 4 + count]
        offset = (addr_hi << 8) | addr_lo

        if rtype == 0x00:
            base = upper + offset
            for index, value in enumerate(payload):
                address = base + index
                if address in chunks and chunks[address] != value:
                    raise FlashError(f"HEX line {line_no}: conflicting data at 0x{address:08X}")
                chunks[address] = value
        elif rtype == 0x01:
            seen_eof = True
        elif rtype == 0x02:
            _require_len(payload, 2, line_no, "extended segment address")
            upper = int.from_bytes(payload, "big") << 4
        elif rtype == 0x04:
            _require_len(payload, 2, line_no, "extended linear address")
            upper = int.from_bytes(payload, "big") << 16
        elif rtype in (0x03, 0x05):
            _require_len(payload, 4, line_no, "start address")
        else:
            raise FlashError(f"HEX line {line_no}: unsupported record type 0x{rtype:02X}")

    if not seen_eof:
        raise FlashError("HEX file has no end-of-file record (truncated download?)")
    if not chunks:
        raise FlashError("HEX file contains no data records")
    return _merge(chunks)


def _require_len(payload: bytes, expected: int, line_no: int, what: str) -> None:
    if len(payload) != expected:
        raise FlashError(f"HEX line {line_no}: {what} record must carry {expected} bytes")


def _merge(chunks: Dict[int, int]) -> List[Segment]:
    segments: List[Segment] = []
    start: Optional[int] = None
    buffer = bytearray()
    previous = None
    for address in sorted(chunks):
        if previous is not None and address != previous + 1:
            segments.append(Segment(start, bytes(buffer)))  # type: ignore[arg-type]
            buffer = bytearray()
            start = None
        if start is None:
            start = address
        buffer.append(chunks[address])
        previous = address
    segments.append(Segment(start, bytes(buffer)))  # type: ignore[arg-type]
    return segments


def validate_segments_for_device(segments: Sequence[Segment], device: XMCDevice) -> None:
    """Refuse images that would write outside the selected part's flash."""
    windows = [
        (base, base + device.flash_bytes) for base in (FLASH_CACHED_BASE, FLASH_UNCACHED_BASE)
    ]
    for segment in segments:
        if not any(lo <= segment.address and segment.end <= hi for lo, hi in windows):
            raise FlashError(
                f"Image range 0x{segment.address:08X}-0x{segment.end - 1:08X} lies outside "
                f"the {device.flash_bytes // 1024} KiB flash of {device.jlink_name}. "
                "Wrong file, or wrong --device?"
            )


# ---------------------------------------------------------------------------
# J-Link Commander
# ---------------------------------------------------------------------------

RunFn = Callable[[List[str]], "subprocess.CompletedProcess[str]"]

JLINK_EXE_NAMES = ("JLinkExe", "JLink.exe", "JLink")
JLINK_SEARCH_GLOBS = (
    "/opt/SEGGER/JLink*/JLinkExe",
    "/Applications/SEGGER/JLink*/JLinkExe",
    r"C:\Program Files\SEGGER\JLink*\JLink.exe",
    r"C:\Program Files (x86)\SEGGER\JLink*\JLink.exe",
)

# Lines J-Link Commander prints when a step failed. ``-ExitOnError 1`` covers
# most cases through the exit code; these catch the ones it reports as text.
_FAILURE_MARKERS = (
    "Cannot connect to target",
    "Connecting to J-Link via USB...FAILED",
    "Could not connect to J-Link",
    "Error while programming flash",
    "Failed to open file",
    "Writing target memory failed",
    "Could not read memory",
)

_SERIAL_RE = re.compile(r"Serial number:\s*(\d+)", re.IGNORECASE)
_PRODUCT_RE = re.compile(r"ProductName:\s*([^,\r\n]+)", re.IGNORECASE)


def find_jlink_exe(explicit: Optional[str] = None) -> str:
    """Locate J-Link Commander, preferring an explicit path, then PATH."""
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise FlashError(f"J-Link Commander not found at {explicit}")
    for name in JLINK_EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for pattern in JLINK_SEARCH_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]  # highest installed version
    raise FlashError(
        "SEGGER J-Link Commander (JLinkExe) was not found. Install the "
        '"J-Link Software and Documentation Pack" for your platform from '
        "https://www.segger.com/downloads/jlink/ (on a Pi 5: the Linux ARM 64-bit "
        ".deb), or pass its location with --jlink."
    )


@dataclass(frozen=True)
class Probe:
    """One J-Link visible on USB."""

    serial: str
    product: str


def parse_emu_list(output: str) -> List[Probe]:
    """Extract probes from ``ShowEmuList`` output."""
    probes = []
    for line in output.splitlines():
        serial = _SERIAL_RE.search(line)
        if serial:
            product = _PRODUCT_RE.search(line)
            probes.append(Probe(serial.group(1), product.group(1).strip() if product else "J-Link"))
    return probes


def parse_mem32(output: str, address: int) -> Optional[int]:
    """Return the 32-bit word J-Link printed for ``mem32 <address> 1``."""
    match = re.search(rf"{address:08X}\s*=\s*([0-9A-Fa-f]{{8}})", output, re.IGNORECASE)
    return int(match.group(1), 16) if match else None


def _default_run(cmd: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed executable, no shell
        cmd, capture_output=True, text=True, timeout=300, check=False
    )


class JLinkCommander:
    """Thin, testable wrapper around non-interactive ``JLinkExe`` runs."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        exe: str,
        workdir: Path,
        *,
        interface: str = "SWD",
        speed_khz: int = 4000,
        usb_serial: Optional[str] = None,
        run: RunFn = _default_run,
    ):
        if any(ch.isspace() for ch in str(workdir)):
            # J-Link Commander's script parser splits on whitespace.
            raise FlashError(
                f"Working directory {workdir} contains whitespace, which J-Link "
                "Commander cannot handle. Set TMPDIR to a path without spaces."
            )
        self.exe = exe
        self.workdir = workdir
        self.interface = interface
        self.speed_khz = speed_khz
        self.usb_serial = usb_serial
        self._run = run
        self._script_counter = 0

    def run_script(self, commands: Sequence[str], *, device: Optional[str]) -> str:
        """Run commands in J-Link Commander and return its combined output.

        ``device=None`` runs without connecting to a target (for
        ``ShowEmuList``); otherwise J-Link auto-connects to that device first.
        """
        self._script_counter += 1
        script = self.workdir / f"step{self._script_counter}.jlink"
        script.write_text("\n".join([*commands, "exit"]) + "\n", encoding="ascii")

        cmd = [self.exe, "-NoGui", "1", "-ExitOnError", "1"]
        if self.usb_serial:
            cmd += ["-USB", self.usb_serial]
        if device is not None:
            cmd += [
                "-Device", device,
                "-If", self.interface,
                "-Speed", str(self.speed_khz),
                "-AutoConnect", "1",
            ]  # fmt: skip
        cmd += ["-CommanderScript", str(script)]

        try:
            result = self._run(cmd)
        except FileNotFoundError as error:
            raise FlashError(f"Could not start {self.exe}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise FlashError("J-Link Commander did not finish within 5 minutes") from error

        output = (result.stdout or "") + (result.stderr or "")
        marker = next((m for m in _FAILURE_MARKERS if m.lower() in output.lower()), None)
        if result.returncode != 0 or marker:
            reason = marker or f"exit code {result.returncode}"
            raise FlashError(f"J-Link Commander failed ({reason}).\n{_tail(output)}")
        return output

    def list_probes(self) -> List[Probe]:
        """Return every J-Link visible on USB."""
        return parse_emu_list(self.run_script(["ShowEmuList"], device=None))

    def read_idchip(self) -> Optional[int]:
        """Connect as a generic Cortex-M4 and read ``SCU_IDCHIP``."""
        output = self.run_script([f"mem32 0x{SCU_IDCHIP_ADDRESS:08X} 1"], device="Cortex-M4")
        return parse_mem32(output, SCU_IDCHIP_ADDRESS)

    def flash(
        self, device: XMCDevice, hex_path: Path, segments: Sequence[Segment], *, verify: bool
    ) -> None:
        """Erase/program ``hex_path``, optionally read back and compare, then run it."""
        commands = ["r", "h", f"loadfile {hex_path}"]
        readbacks = []
        if verify:
            for index, segment in enumerate(segments):
                target = self.workdir / f"readback{index}.bin"
                readbacks.append((segment, target))
                commands.append(
                    f"savebin {target}, 0x{segment.address:08X}, 0x{len(segment.data):X}"
                )
        commands += ["r", "g"]
        self.run_script(commands, device=device.jlink_name)

        for segment, target in readbacks:
            compare_readback(segment, target)


def compare_readback(segment: Segment, path: Path) -> None:
    """Raise if the bytes J-Link read back differ from the image."""
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise FlashError(
            f"Read-back of 0x{segment.address:08X} produced no file ({error}); "
            "the flash was NOT verified."
        ) from error
    if actual == segment.data:
        return
    if len(actual) != len(segment.data):
        raise FlashError(
            f"Read-back of 0x{segment.address:08X} returned {len(actual)} bytes, "
            f"expected {len(segment.data)}. The flash was NOT verified."
        )
    first = next(i for i, (a, b) in enumerate(zip(actual, segment.data)) if a != b)
    raise FlashError(
        f"Verify FAILED at 0x{segment.address + first:08X}: wrote "
        f"0x{segment.data[first]:02X}, read 0x{actual[first]:02X}. Re-run the flash."
    )


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


__all__ = [
    "DEFAULT_DEVICE_FOR_FAMILY",
    "FLASH_CACHED_BASE",
    "FLASH_UNCACHED_BASE",
    "FlashError",
    "JLinkCommander",
    "Probe",
    "SCU_IDCHIP_ADDRESS",
    "Segment",
    "XMCDevice",
    "XMC_DEVICES",
    "compare_readback",
    "family_from_idchip",
    "find_jlink_exe",
    "parse_emu_list",
    "parse_intel_hex",
    "parse_mem32",
    "resolve_device",
    "validate_segments_for_device",
]
