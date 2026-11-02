# shellcheck shell=bash
#
# Raspberry Pi OS ships Picamera2 and libcamera's Python bindings only for the
# system interpreter (/usr/bin/python3), compiled for its exact minor version
# (3.13 on Trixie, 3.11 on Bookworm). OpenFlight's .venv must therefore be
# built on that interpreter with system site-packages visible; a venv on any
# other interpreter cannot import the camera stack.
#
# The repository deliberately has no .python-version pin, so once .venv is
# built here every later `uv sync` / `uv run` reuses it instead of rebuilding.
#
# Source this file; it only defines functions.

# Print "<base prefix> <major>.<minor>" for an interpreter. For a venv this is
# the install it was created from; for a system interpreter it is itself.
_openflight_python_identity() {
    "$1" -c 'import sys; print(sys.base_prefix, "%d.%d" % sys.version_info[:2])'
}

# Succeed when VENV_DIR was built on SYSTEM_PYTHON with system site-packages.
# Usage: openflight_venv_matches_system_python VENV_DIR SYSTEM_PYTHON
openflight_venv_matches_system_python() {
    local venv_dir="$1"
    local system_python="$2"
    local venv_identity system_identity

    [ -x "$venv_dir/bin/python" ] || return 1
    grep -Eq '^include-system-site-packages[[:space:]]*=[[:space:]]*true[[:space:]]*$' \
        "$venv_dir/pyvenv.cfg" 2>/dev/null || return 1
    venv_identity=$(_openflight_python_identity "$venv_dir/bin/python" 2>/dev/null) || return 1
    system_identity=$(_openflight_python_identity "$system_python" 2>/dev/null) || return 1
    [ -n "$venv_identity" ] && [ "$venv_identity" = "$system_identity" ]
}

# (Re)build VENV_DIR on SYSTEM_PYTHON with system site-packages, unless it
# already matches. Installed packages are discarded on a rebuild; the caller
# reinstalls them.
# Usage: openflight_ensure_system_python_venv VENV_DIR SYSTEM_PYTHON
openflight_ensure_system_python_venv() {
    local venv_dir="$1"
    local system_python="$2"

    if openflight_venv_matches_system_python "$venv_dir" "$system_python"; then
        return 0
    fi
    if [ ! -x "$system_python" ]; then
        echo "System Python not found at $system_python" >&2
        return 1
    fi
    uv venv --clear --system-site-packages --python "$system_python" "$venv_dir"
}
