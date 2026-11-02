"""Helpers for tests that drive repository shell scripts through bash."""

import os
import shlex
import stat
import subprocess
from pathlib import Path


def bash_path(path: Path) -> str:
    """Return PATH as the `bash` on PATH sees it (WSL or Git Bash on Windows)."""
    resolved = str(path.resolve())
    if os.name != "nt":
        return resolved
    converted = subprocess.run(
        ["bash", "-lc", f"wslpath -u {shlex.quote(resolved.replace(chr(92), '/'))}"],
        capture_output=True,
        text=True,
        check=False,
    )
    mapped = converted.stdout.strip()
    if converted.returncode == 0 and mapped:
        return mapped
    posix = Path(resolved).as_posix()
    return f"/{posix[0].lower()}{posix[2:]}"


def write_executable(path: Path, contents: str) -> None:
    """Write a script with Unix newlines and mark it executable."""
    path.write_text(contents, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
