"""Tests for flashing the OPS243 (XMC4500) through a SEGGER J-Link."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import firmware.flash_ops243 as flash_cli
from openflight.ops243_jlink import (
    SCU_IDCHIP_ADDRESS,
    XMC_DEVICES,
    FlashError,
    JLinkCommander,
    Probe,
    Segment,
    compare_readback,
    family_from_idchip,
    find_jlink_exe,
    parse_emu_list,
    parse_intel_hex,
    parse_mem32,
    resolve_device,
    validate_segments_for_device,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(rtype: int, address: int, payload: bytes) -> str:
    body = bytes([len(payload), (address >> 8) & 0xFF, address & 0xFF, rtype]) + payload
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def make_hex(segments: list[Segment], *, eof: bool = True) -> str:
    """Encode segments as Intel HEX with extended linear address records."""
    lines = []
    upper = None
    for segment in segments:
        for offset in range(0, len(segment.data), 16):
            address = segment.address + offset
            if address >> 16 != upper:
                upper = address >> 16
                lines.append(_record(0x04, 0, upper.to_bytes(2, "big")))
            lines.append(_record(0x00, address & 0xFFFF, segment.data[offset : offset + 16]))
    if eof:
        lines.append(_record(0x01, 0, b""))
    return "\n".join(lines) + "\n"


def _flip_checksum(text: str, line_index: int) -> str:
    """Corrupt the checksum byte of one record."""
    lines = text.splitlines()
    line = lines[line_index]
    lines[line_index] = line[:-2] + f"{(int(line[-2:], 16) + 1) & 0xFF:02X}"
    return "\n".join(lines) + "\n"


EMU_LIST_ONE = (
    "SEGGER J-Link Commander V9.78\n"
    "J-Link[0]: Connection: USB, Serial number: 801012345, ProductName: J-Link EDU Mini\n"
)
EMU_LIST_TWO = EMU_LIST_ONE + (
    "J-Link[1]: Connection: USB, Serial number: 600111222, ProductName: J-Link OB-XMC4200\n"
)
IDCHIP_XMC4500 = 0x0004_5002
IDCHIP_XMC4700 = 0x0004_7002


class FakeJLink:
    """Records JLinkExe invocations and answers like J-Link Commander would."""

    def __init__(
        self,
        *,
        emu_list: str = EMU_LIST_ONE,
        idchip: int | None = IDCHIP_XMC4500,
        flash_bytes: dict[int, bytes] | None = None,
        fail_on: str | None = None,
        returncode: int = 0,
    ):
        self.emu_list = emu_list
        self.idchip = idchip
        self.flash_bytes = flash_bytes or {}
        self.fail_on = fail_on
        self.returncode = returncode
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        script = Path(cmd[cmd.index("-CommanderScript") + 1])
        commands = script.read_text().splitlines()
        self.calls.append((cmd, commands))
        out = ["SEGGER J-Link Commander V9.78"]
        if self.fail_on and any(c.startswith(self.fail_on) for c in commands):
            out.append("Cannot connect to target.")
            return subprocess.CompletedProcess(cmd, 1, "\n".join(out), "")
        for command in commands:
            if command == "ShowEmuList":
                out.append(self.emu_list)
            elif command.startswith("mem32") and self.idchip is not None:
                out.append(f"{SCU_IDCHIP_ADDRESS:08X} = {self.idchip:08X}")
            elif command.startswith("savebin"):
                match = re.match(r"savebin (\S+), 0x([0-9A-F]+), 0x([0-9A-F]+)", command)
                assert match, command
                path, address, size = (
                    match.group(1),
                    int(match.group(2), 16),
                    int(match.group(3), 16),
                )
                Path(path).write_bytes(self.flash_bytes.get(address, b"\xff" * size))
            elif command.startswith("loadfile"):
                out.append("O.K.")
        return subprocess.CompletedProcess(cmd, self.returncode, "\n".join(out), "")

    def scripts(self) -> list[list[str]]:
        return [commands for _, commands in self.calls]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Intel HEX parsing
# ---------------------------------------------------------------------------


class TestParseIntelHex:
    def test_round_trips_segments_across_64k_boundary(self):
        data = bytes(range(256)) * 300  # 76,800 bytes, crosses a 64 KiB page
        segments = parse_intel_hex(make_hex([Segment(0x0C00_0000, data)]))
        assert segments == [Segment(0x0C00_0000, data)]

    def test_keeps_gaps_as_separate_segments(self):
        image = [Segment(0x0C00_0000, b"\x01" * 32), Segment(0x0C01_0000, b"\x02" * 8)]
        assert parse_intel_hex(make_hex(image)) == image

    def test_merges_out_of_order_records(self):
        lines = make_hex([Segment(0x0800_0000, bytes(32))]).splitlines()
        reordered = [lines[0], lines[2], lines[1], lines[3]]
        assert parse_intel_hex("\n".join(reordered)) == [Segment(0x0800_0000, bytes(32))]

    def test_extended_segment_address_record(self):
        text = "\n".join(
            [
                _record(0x02, 0, b"\x10\x00"),
                _record(0x00, 0x0010, b"\xaa\xbb"),
                _record(0x01, 0, b""),
            ]
        )
        assert parse_intel_hex(text) == [Segment(0x10010, b"\xaa\xbb")]

    def test_start_address_records_are_accepted(self):
        text = make_hex([Segment(0x0C00_0000, b"\x00" * 4)]).replace(
            _record(0x01, 0, b""),
            _record(0x05, 0, b"\x0c\x00\x02\x01") + "\n" + _record(0x01, 0, b""),
        )
        assert parse_intel_hex(text) == [Segment(0x0C00_0000, b"\x00" * 4)]

    def test_tolerates_crlf_and_blank_lines(self):
        text = make_hex([Segment(0x0C00_0000, b"\x11" * 4)]).replace("\n", "\r\n\r\n")
        assert parse_intel_hex(text) == [Segment(0x0C00_0000, b"\x11" * 4)]

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda t: t.replace(":", "", 1), "does not start"),
            (lambda t: _flip_checksum(t, 1), "checksum"),
            (lambda t: t.replace(":02", ":03", 1), "byte count"),
            (lambda t: t + _record(0x00, 0, b"\x01") + "\n", "after the end-of-file"),
            (lambda t: t.replace(t.splitlines()[1], ":10ZZ"), "invalid hex"),
            (lambda t: ":00\n" + t, "too short"),
        ],
    )
    def test_rejects_corrupt_files(self, mutate, message):
        text = make_hex([Segment(0x0C00_0000, b"\x01\x02")])
        with pytest.raises(FlashError, match=message):
            parse_intel_hex(mutate(text))

    def test_rejects_truncated_download(self):
        with pytest.raises(FlashError, match="end-of-file"):
            parse_intel_hex(make_hex([Segment(0x0C00_0000, b"\x01" * 4)], eof=False))

    def test_rejects_empty_image(self):
        with pytest.raises(FlashError, match="no data"):
            parse_intel_hex(_record(0x01, 0, b""))

    def test_rejects_unknown_record_type(self):
        with pytest.raises(FlashError, match="unsupported record type 0x07"):
            parse_intel_hex(_record(0x07, 0, b"") + "\n" + _record(0x01, 0, b""))

    def test_rejects_conflicting_overlap_but_allows_identical(self):
        same = _record(0x00, 0, b"\x01")
        assert parse_intel_hex("\n".join([same, same, _record(0x01, 0, b"")])) == [
            Segment(0, b"\x01")
        ]
        with pytest.raises(FlashError, match="conflicting"):
            parse_intel_hex("\n".join([same, _record(0x00, 0, b"\x02"), _record(0x01, 0, b"")]))

    def test_rejects_malformed_extended_address(self):
        with pytest.raises(FlashError, match="extended linear"):
            parse_intel_hex(_record(0x04, 0, b"\x0c") + "\n" + _record(0x01, 0, b""))


# ---------------------------------------------------------------------------
# Device identification and image validation
# ---------------------------------------------------------------------------


class TestDevice:
    @pytest.mark.parametrize(
        ("idchip", "family"),
        [
            (IDCHIP_XMC4500, "XMC4500"),
            (0x0004_5004, "XMC4500"),
            (IDCHIP_XMC4700, "XMC4700"),
            (0x0004_8002, "XMC4800"),
            (0x0001_3000, None),  # an XMC1300-style ID is not an XMC4
            (0x0004_9000, None),
            (0, None),
        ],
    )
    def test_family_from_idchip(self, idchip, family):
        assert family_from_idchip(idchip) == family

    def test_auto_picks_largest_part_of_detected_family(self):
        assert resolve_device("auto", "XMC4500", skip_chip_check=False).jlink_name == "XMC4500-1024"
        assert resolve_device("auto", "XMC4700", skip_chip_check=False).jlink_name == "XMC4700-2048"

    def test_auto_requires_detection(self):
        with pytest.raises(FlashError, match="Could not identify"):
            resolve_device("auto", None, skip_chip_check=True)

    def test_auto_refuses_non_ops_family(self):
        with pytest.raises(FlashError, match="XMC4800"):
            resolve_device("auto", "XMC4800", skip_chip_check=False)

    def test_explicit_matching_device(self):
        assert (
            resolve_device("XMC4500-768", "XMC4500", skip_chip_check=False).flash_bytes
            == 768 * 1024
        )

    def test_explicit_device_contradicting_chip_is_refused_even_with_skip(self):
        with pytest.raises(FlashError, match="Refusing"):
            resolve_device("XMC4700-2048", "XMC4500", skip_chip_check=True)

    def test_explicit_device_without_detection_needs_skip(self):
        with pytest.raises(FlashError, match="--skip-chip-check"):
            resolve_device("XMC4500-1024", None, skip_chip_check=False)
        assert resolve_device("XMC4500-1024", None, skip_chip_check=True).family == "XMC4500"

    def test_unknown_explicit_device(self):
        with pytest.raises(FlashError, match="Unknown device"):
            resolve_device("XMC1100-64", "XMC4500", skip_chip_check=False)

    @pytest.mark.parametrize("base", [0x0800_0000, 0x0C00_0000])
    def test_accepts_image_in_either_flash_alias(self, base):
        validate_segments_for_device([Segment(base, bytes(1024))], XMC_DEVICES["XMC4500-1024"])

    def test_accepts_image_ending_exactly_at_flash_end(self):
        device = XMC_DEVICES["XMC4500-512"]
        validate_segments_for_device(
            [Segment(0x0C00_0000 + device.flash_bytes - 4, bytes(4))], device
        )

    @pytest.mark.parametrize(
        "segment",
        [
            Segment(0x0C00_0000 + 512 * 1024 - 2, bytes(4)),  # straddles the end
            Segment(0x2000_0000, bytes(4)),  # RAM
            Segment(0x0000_0000, bytes(4)),  # boot ROM
        ],
    )
    def test_rejects_image_outside_flash(self, segment):
        with pytest.raises(FlashError, match="outside"):
            validate_segments_for_device([segment], XMC_DEVICES["XMC4500-512"])


# ---------------------------------------------------------------------------
# J-Link Commander output parsing and wrapper
# ---------------------------------------------------------------------------


class TestJLinkParsing:
    def test_parse_emu_list(self):
        assert parse_emu_list(EMU_LIST_TWO) == [
            Probe("801012345", "J-Link EDU Mini"),
            Probe("600111222", "J-Link OB-XMC4200"),
        ]

    def test_parse_emu_list_empty(self):
        assert parse_emu_list("SEGGER J-Link Commander\nNo J-Links connected\n") == []

    def test_parse_mem32(self):
        output = "J-Link>mem32 0x50004004 1\n50004004 = 00045002 \n"
        assert parse_mem32(output, SCU_IDCHIP_ADDRESS) == IDCHIP_XMC4500

    def test_parse_mem32_missing(self):
        assert parse_mem32("Could not read memory.\n", SCU_IDCHIP_ADDRESS) is None


class TestFindJLink:
    def test_explicit_path(self, tmp_path):
        exe = tmp_path / "JLinkExe"
        exe.write_text("")
        assert find_jlink_exe(str(exe)) == str(exe)

    def test_explicit_missing_path(self, tmp_path):
        with pytest.raises(FlashError, match="not found at"):
            find_jlink_exe(str(tmp_path / "nope"))

    def test_path_lookup(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/JLinkExe" if name == "JLinkExe" else None
        )
        assert find_jlink_exe() == "/usr/bin/JLinkExe"

    def test_picks_newest_install_from_search_globs(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: None)
        for version in ("V794", "V978"):
            (tmp_path / f"JLink_{version}").mkdir()
            (tmp_path / f"JLink_{version}" / "JLinkExe").write_text("")
        monkeypatch.setattr(
            "openflight.ops243_jlink.JLINK_SEARCH_GLOBS", (str(tmp_path / "JLink*" / "JLinkExe"),)
        )
        assert find_jlink_exe().endswith("JLink_V978/JLinkExe")

    def test_not_installed_points_to_segger(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("openflight.ops243_jlink.JLINK_SEARCH_GLOBS", ())
        with pytest.raises(FlashError, match="segger.com/downloads/jlink"):
            find_jlink_exe()


class TestJLinkCommander:
    def test_list_probes_runs_without_target(self, workdir):
        fake = FakeJLink()
        probes = JLinkCommander("JLinkExe", workdir, run=fake).list_probes()
        assert probes == [Probe("801012345", "J-Link EDU Mini")]
        cmd, commands = fake.calls[0]
        assert "-Device" not in cmd and "-AutoConnect" not in cmd
        assert commands == ["ShowEmuList", "exit"]
        assert cmd[:5] == ["JLinkExe", "-NoGui", "1", "-ExitOnError", "1"]

    def test_read_idchip_connects_as_generic_cortex_m4(self, workdir):
        fake = FakeJLink()
        jlink = JLinkCommander(
            "JLinkExe", workdir, interface="JTAG", speed_khz=1000, usb_serial="801012345", run=fake
        )
        assert jlink.read_idchip() == IDCHIP_XMC4500
        cmd, commands = fake.calls[0]
        assert cmd[cmd.index("-Device") + 1] == "Cortex-M4"
        assert cmd[cmd.index("-If") + 1] == "JTAG"
        assert cmd[cmd.index("-Speed") + 1] == "1000"
        assert cmd[cmd.index("-USB") + 1] == "801012345"
        assert commands == ["mem32 0x50004004 1", "exit"]

    def test_nonzero_exit_raises_with_output_tail(self, workdir):
        fake = FakeJLink(returncode=1)
        with pytest.raises(FlashError, match="exit code 1"):
            JLinkCommander("JLinkExe", workdir, run=fake).list_probes()

    def test_failure_marker_raises_even_on_zero_exit(self, workdir):
        def run(cmd):
            return subprocess.CompletedProcess(cmd, 0, "Cannot connect to target.\n", "")

        with pytest.raises(FlashError, match="Cannot connect to target"):
            JLinkCommander("JLinkExe", workdir, run=run).read_idchip()

    def test_missing_executable(self, workdir):
        def run(cmd):
            raise FileNotFoundError(cmd[0])

        with pytest.raises(FlashError, match="Could not start"):
            JLinkCommander("JLinkExe", workdir, run=run).list_probes()

    def test_timeout(self, workdir):
        def run(cmd):
            raise subprocess.TimeoutExpired(cmd, 300)

        with pytest.raises(FlashError, match="5 minutes"):
            JLinkCommander("JLinkExe", workdir, run=run).list_probes()

    def test_rejects_workdir_with_spaces(self, tmp_path):
        with pytest.raises(FlashError, match="whitespace"):
            JLinkCommander("JLinkExe", tmp_path / "has space", run=FakeJLink())

    def test_flash_programs_reads_back_and_restarts(self, workdir):
        image = [Segment(0x0C00_0000, b"\x10" * 64), Segment(0x0C00_8000, b"\x20" * 16)]
        fake = FakeJLink(flash_bytes={s.address: s.data for s in image})
        hex_path = workdir / "image.hex"
        JLinkCommander("JLinkExe", workdir, run=fake).flash(
            XMC_DEVICES["XMC4500-1024"], hex_path, image, verify=True
        )
        cmd, commands = fake.calls[0]
        assert cmd[cmd.index("-Device") + 1] == "XMC4500-1024"
        assert commands[:3] == ["r", "h", f"loadfile {hex_path}"]
        assert commands[3] == f"savebin {workdir / 'readback0.bin'}, 0x0C000000, 0x40"
        assert commands[4] == f"savebin {workdir / 'readback1.bin'}, 0x0C008000, 0x10"
        assert commands[-3:] == ["r", "g", "exit"]

    def test_flash_without_verify_skips_readback(self, workdir):
        fake = FakeJLink()
        JLinkCommander("JLinkExe", workdir, run=fake).flash(
            XMC_DEVICES["XMC4500-1024"],
            workdir / "image.hex",
            [Segment(0x0C00_0000, b"\x01")],
            verify=False,
        )
        assert not any(c.startswith("savebin") for c in fake.scripts()[0])

    def test_flash_detects_mismatch(self, workdir):
        image = [Segment(0x0C00_0000, b"\x10" * 8)]
        fake = FakeJLink(flash_bytes={0x0C00_0000: b"\x10\x10\x10\xef\x10\x10\x10\x10"})
        with pytest.raises(FlashError, match=r"Verify FAILED at 0x0C000003: wrote 0x10, read 0xEF"):
            JLinkCommander("JLinkExe", workdir, run=fake).flash(
                XMC_DEVICES["XMC4500-1024"], workdir / "image.hex", image, verify=True
            )


class TestCompareReadback:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FlashError, match="NOT verified"):
            compare_readback(Segment(0, b"\x00"), tmp_path / "missing.bin")

    def test_short_read(self, tmp_path):
        path = tmp_path / "short.bin"
        path.write_bytes(b"\x00")
        with pytest.raises(FlashError, match="returned 1 bytes, expected 2"):
            compare_readback(Segment(0, b"\x00\x00"), path)

    def test_match(self, tmp_path):
        path = tmp_path / "ok.bin"
        path.write_bytes(b"\x01\x02")
        compare_readback(Segment(0, b"\x01\x02"), path)


# ---------------------------------------------------------------------------
# Command-line workflow
# ---------------------------------------------------------------------------

IMAGE = [Segment(0x0C00_0000, bytes(range(256)) * 4)]


@pytest.fixture
def hex_file(tmp_path: Path) -> Path:
    path = tmp_path / "OPS243-A v1.3.2.hex"  # space in name is staged away
    path.write_text(make_hex(IMAGE))
    return path


@pytest.fixture
def jlink_on_path(monkeypatch):
    monkeypatch.setattr(flash_cli, "find_jlink_exe", lambda explicit=None: "JLinkExe")


class CliHarness:
    """Runs ``flash_cli.main`` against a FakeJLink."""

    def __init__(
        self, fake: FakeJLink, versions: list[str | None] | None = None, answer: str = "FLASH"
    ):
        self.fake = fake
        self.versions = list(versions or [])
        self.version_ports: list[str | None] = []
        self.answer = answer
        self.prompts: list[str] = []
        self.sleeps: list[float] = []

    def factory(self, exe, workdir, **kwargs):
        return JLinkCommander(exe, workdir, run=self.fake, **kwargs)

    def read_version(self, port):
        self.version_ports.append(port)
        return self.versions.pop(0) if self.versions else None

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answer

    def run(self, argv: list[str]) -> int:
        return flash_cli.main(
            argv,
            commander_factory=self.factory,
            version_reader=self.read_version,
            ask=self.ask,
            sleep=self.sleeps.append,
        )


@pytest.mark.usefixtures("jlink_on_path")
class TestCli:
    def test_full_flash(self, hex_file, capsys):
        harness = CliHarness(
            FakeJLink(flash_bytes={0x0C00_0000: IMAGE[0].data}), ["1.3.1", "1.3.2"]
        )
        assert harness.run([str(hex_file), "--port", "/dev/ttyAMA0"]) == 0
        out = capsys.readouterr().out
        assert "Probe:   J-Link EDU Mini #801012345" in out
        assert "-> XMC4500" in out
        assert "Device:  XMC4500-1024" in out
        assert "verified by read-back" in out
        assert "firmware 1.3.1 -> 1.3.2" in out
        assert harness.version_ports == ["/dev/ttyAMA0", "/dev/ttyAMA0"]
        flash_script = harness.fake.scripts()[2]
        loadfile = next(c for c in flash_script if c.startswith("loadfile"))
        assert " " not in loadfile.split(" ", 1)[1]  # staged copy, no spaces
        assert harness.prompts == ["\nType FLASH to begin: "]

    def test_probe_never_writes(self, capsys):
        harness = CliHarness(FakeJLink())
        assert harness.run(["--probe"]) == 0
        assert "Nothing was written" in capsys.readouterr().out
        assert not any(
            c.startswith(("loadfile", "savebin")) for s in harness.fake.scripts() for c in s
        )
        assert harness.prompts == [] and harness.version_ports == []

    def test_cancel(self, hex_file):
        harness = CliHarness(FakeJLink(), answer="flash")
        assert harness.run([str(hex_file)]) == 2
        assert len(harness.fake.calls) == 2  # emu list + chip id only

    def test_yes_skips_prompt_and_no_version_check_skips_serial(self, hex_file):
        harness = CliHarness(FakeJLink(flash_bytes={0x0C00_0000: IMAGE[0].data}))
        assert harness.run([str(hex_file), "--yes", "--no-version-check"]) == 0
        assert harness.prompts == [] and harness.version_ports == [] and harness.sleeps == []

    def test_verify_failure_exits_1(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(flash_bytes={}))  # reads back erased 0xFF
        assert harness.run([str(hex_file), "--yes", "--no-version-check"]) == 1
        assert "Verify FAILED" in capsys.readouterr().err

    def test_no_verify(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(flash_bytes={}))
        assert harness.run([str(hex_file), "--yes", "--no-version-check", "--no-verify"]) == 0
        assert "(unverified)" in capsys.readouterr().out

    def test_no_probe_connected(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(emu_list="No J-Links\n"))
        assert harness.run([str(hex_file), "--yes"]) == 1
        assert "No J-Link found" in capsys.readouterr().err

    def test_several_probes_need_usb_serial(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(emu_list=EMU_LIST_TWO))
        assert harness.run([str(hex_file), "--yes"]) == 1
        assert "--usb" in capsys.readouterr().err

    def test_usb_serial_selects_probe(self, hex_file):
        harness = CliHarness(
            FakeJLink(emu_list=EMU_LIST_TWO, flash_bytes={0x0C00_0000: IMAGE[0].data})
        )
        assert (
            harness.run([str(hex_file), "--yes", "--no-version-check", "--usb", "600111222"]) == 0
        )
        cmd = harness.fake.calls[1][0]
        assert cmd[cmd.index("-USB") + 1] == "600111222"

    def test_unknown_usb_serial(self, hex_file, capsys):
        harness = CliHarness(FakeJLink())
        assert harness.run([str(hex_file), "--yes", "--usb", "1"]) == 1
        assert "is not connected" in capsys.readouterr().err

    def test_unpowered_target_gives_power_hint(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(fail_on="mem32"))
        assert harness.run([str(hex_file), "--yes"]) == 1
        assert "does not power the target" in capsys.readouterr().err

    def test_wrong_family_is_refused_before_writing(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(idchip=IDCHIP_XMC4700))
        assert harness.run([str(hex_file), "--yes", "--device", "XMC4500-1024"]) == 1
        assert "Refusing" in capsys.readouterr().err
        assert len(harness.fake.calls) == 2

    def test_xmc4700_board_is_auto_detected(self, hex_file, capsys):
        harness = CliHarness(
            FakeJLink(idchip=IDCHIP_XMC4700, flash_bytes={0x0C00_0000: IMAGE[0].data})
        )
        assert harness.run([str(hex_file), "--yes", "--no-version-check"]) == 0
        assert "Device:  XMC4700-2048" in capsys.readouterr().out

    def test_image_too_large_for_device_is_refused(self, tmp_path, capsys):
        path = tmp_path / "big.hex"
        path.write_text(make_hex([Segment(0x0C00_0000 + 512 * 1024, b"\x01")]))
        harness = CliHarness(FakeJLink())
        assert harness.run([str(path), "--yes", "--device", "XMC4500-512"]) == 1
        assert "outside" in capsys.readouterr().err
        assert len(harness.fake.calls) == 2

    def test_unreadable_version_after_flash_is_not_an_error(self, hex_file, capsys):
        harness = CliHarness(FakeJLink(flash_bytes={0x0C00_0000: IMAGE[0].data}), ["1.3.1"])
        assert harness.run([str(hex_file), "--yes"]) == 0
        assert "power-cycle the radar" in capsys.readouterr().out
        assert len(harness.version_ports) == 1 + flash_cli.POST_FLASH_VERSION_ATTEMPTS

    def test_invalid_hex_is_rejected_before_touching_probe(self, tmp_path):
        path = tmp_path / "bad.hex"
        path.write_text(":00000001FF\n")
        harness = CliHarness(FakeJLink())
        with pytest.raises(SystemExit):
            harness.run([str(path)])
        assert harness.fake.calls == []

    def test_image_required_without_probe(self):
        with pytest.raises(SystemExit):
            CliHarness(FakeJLink()).run([])
