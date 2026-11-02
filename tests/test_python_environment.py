"""Tests for the Python interpreter OpenFlight's .venv is built on (issue #326).

Raspberry Pi OS ships Picamera2/libcamera bindings only for its system Python,
so the repo must not pin a different interpreter, and setup.sh must build the
Pi venv on the system Python with system site-packages.
"""

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.shell_harness import bash_path, write_executable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "setup" / "system-python-venv.sh"
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup" / "setup.sh"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _fake_python(path: Path, identity: str) -> None:
    """An interpreter stub that reports `<base prefix> <major>.<minor>`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_executable(path, f"#!/usr/bin/env bash\necho {shlex.quote(identity)}\n")


def _make_venv(
    root: Path, identity: str, *, system_site_packages: bool = True, cfg: bool = True
) -> Path:
    venv = root / "venv"
    _fake_python(venv / "bin" / "python", identity)
    if cfg:
        flag = "true" if system_site_packages else "false"
        (venv / "pyvenv.cfg").write_text(
            f"home = /usr/bin\ninclude-system-site-packages = {flag}\nversion_info = 3.13.5\n",
            encoding="utf-8",
            newline="\n",
        )
    return venv


def _run_helper(
    tmp_path: Path, function: str, venv: Path, system_python: Path, *, uv_exit: int = 0
):
    """Run a helper function with a stub `uv` that records its arguments."""
    bin_dir = tmp_path / "bin"
    uv_log = tmp_path / "uv-calls"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "uv",
        f'#!/usr/bin/env bash\necho "$*" >> {shlex.quote(bash_path(uv_log))}\nexit {uv_exit}\n',
    )
    # One quoted command string: WSL's bash.exe drops positional `bash -c` args.
    script = (
        f'PATH={shlex.quote(bash_path(bin_dir))}:"$PATH"; '
        f"source {shlex.quote(bash_path(HELPER))}; "
        f"{function} {shlex.quote(bash_path(venv))} {shlex.quote(bash_path(system_python))}"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    calls = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
    return result, calls


@pytest.fixture
def system_python(tmp_path: Path) -> Path:
    path = tmp_path / "usr" / "bin" / "python3"
    _fake_python(path, "/usr 3.13")
    return path


def test_repo_does_not_pin_a_python_version():
    """A pin makes every plain `uv sync` rebuild .venv away from the Pi's system Python."""
    assert not (REPO_ROOT / ".python-version").exists()


def test_ci_tests_the_python_raspberry_pi_os_trixie_ships():
    workflow = (REPO_ROOT / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version: \[(.*?)\]", workflow).group(1)

    assert '"3.13"' in matrix


def test_setup_minimum_python_matches_requires_python():
    requires = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["requires-python"]
    setup = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert requires == ">=3.10"
    assert "sys.version_info < (3, 10)" in setup
    assert "3.9" not in setup


def test_setup_builds_pi_venv_on_system_python_and_reports_venv_python():
    setup = SETUP_SCRIPT.read_text(encoding="utf-8")
    venv_block = setup[
        setup.index("# Create virtual environment") : setup.index(
            "# Install Python dependencies"
        )
    ]

    assert 'source "$SCRIPT_DIR/system-python-venv.sh"' in venv_block
    assert "openflight_ensure_system_python_venv .venv /usr/bin/python3" in venv_block
    assert 'if [[ "$PLATFORM" == "pi" ]]; then' in venv_block
    assert "Virtual environment uses Python" in venv_block


def test_uv_python_workaround_is_not_spread_across_docs_and_wrappers():
    for relative in (
        "docs/setup/splash-screen.md",
        "docs/camera/README.md",
        "scripts/setup/run-openflight.example.sh",
    ):
        assert "UV_PYTHON" not in (REPO_ROOT / relative).read_text(encoding="utf-8"), relative


def test_pi_setup_guide_documents_the_system_python_venv():
    guide = (REPO_ROOT / "docs" / "setup" / "raspberry-pi.md").read_text(encoding="utf-8")
    camera = (REPO_ROOT / "docs" / "camera" / "README.md").read_text(encoding="utf-8")

    assert "### Python Environment" in guide
    assert "--system-site-packages" in guide
    assert "raspberry-pi.md#python-environment" in camera


@needs_bash
def test_helper_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", bash_path(HELPER)], check=True)


@needs_bash
def test_matching_system_venv_is_accepted(tmp_path, system_python):
    venv = _make_venv(tmp_path, "/usr 3.13")

    result, _ = _run_helper(tmp_path, "openflight_venv_matches_system_python", venv, system_python)

    assert result.returncode == 0, result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("identity", "system_site_packages", "cfg"),
    [
        pytest.param("/usr 3.11", True, True, id="older-minor-version"),
        pytest.param("/home/pi/.local/share/uv/python/3.13 3.13", True, True, id="uv-managed"),
        pytest.param("/usr 3.13", False, True, id="no-system-site-packages"),
        pytest.param("/usr 3.13", True, False, id="missing-pyvenv-cfg"),
    ],
)
def test_mismatched_venv_is_rejected(tmp_path, system_python, identity, system_site_packages, cfg):
    venv = _make_venv(tmp_path, identity, system_site_packages=system_site_packages, cfg=cfg)

    result, _ = _run_helper(tmp_path, "openflight_venv_matches_system_python", venv, system_python)

    assert result.returncode == 1


@needs_bash
def test_missing_venv_is_rejected(tmp_path, system_python):
    result, _ = _run_helper(
        tmp_path, "openflight_venv_matches_system_python", tmp_path / "absent", system_python
    )

    assert result.returncode == 1


@needs_bash
def test_ensure_leaves_matching_venv_alone(tmp_path, system_python):
    venv = _make_venv(tmp_path, "/usr 3.13")

    result, calls = _run_helper(
        tmp_path, "openflight_ensure_system_python_venv", venv, system_python
    )

    assert result.returncode == 0, result.stderr
    assert calls == []


@needs_bash
@pytest.mark.parametrize("existing", ["/usr 3.11", None], ids=["wrong-python", "no-venv"])
def test_ensure_rebuilds_on_system_python_with_site_packages(tmp_path, system_python, existing):
    venv = _make_venv(tmp_path, existing) if existing else tmp_path / "venv"

    result, calls = _run_helper(
        tmp_path, "openflight_ensure_system_python_venv", venv, system_python
    )

    assert result.returncode == 0, result.stderr
    assert calls == [
        f"venv --clear --system-site-packages --python {bash_path(system_python)} "
        f"{bash_path(venv)}"
    ]


@needs_bash
def test_ensure_fails_when_uv_fails(tmp_path, system_python):
    result, calls = _run_helper(
        tmp_path,
        "openflight_ensure_system_python_venv",
        tmp_path / "venv",
        system_python,
        uv_exit=3,
    )

    assert result.returncode == 3
    assert len(calls) == 1


@needs_bash
def test_ensure_fails_without_system_python(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        "openflight_ensure_system_python_venv",
        tmp_path / "venv",
        tmp_path / "missing" / "python3",
    )

    assert result.returncode == 1
    assert "System Python not found" in result.stderr
    assert calls == []
