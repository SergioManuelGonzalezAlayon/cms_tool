#!/usr/bin/env python3
"""Update the connected JS320 to firmware 1.1.9.

Run from any directory with:
    .venv/bin/python scripts/update_js320_firmware.py

The script downloads the official firmware package to a temporary directory,
checks the connected JS320 firmware, asks for the device serial as confirmation,
programs it, and verifies the result. Use --yes to skip the confirmation prompt.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen


FIRMWARE_VERSION = "1.1.9"
FIRMWARE_URL = "https://download.joulescope.com/firmware/js320/js320-1_1_9.zip"
DEVICE_PATTERN = re.compile(
    r"^\s*u/js320/([^:\s]+):.*\bfw=([0-9]+(?:\.[0-9]+)+)",
    re.MULTILINE,
)


def repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Could not locate repository root (pyproject.toml).")


def venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        python = root / ".venv" / "Scripts" / "python.exe"
    else:
        python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"Project virtual environment Python not found: {python}")
    return python


def run_info(root: Path, python: Path) -> tuple[str, str]:
    result = subprocess.run(
        [str(python), "-m", "pyjoulescope_driver", "info"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    print(output, end="" if output.endswith("\n") else "\n")
    if result.returncode:
        raise RuntimeError(f"Joulescope info failed with exit code {result.returncode}.")

    devices = DEVICE_PATTERN.findall(output)
    if len(devices) != 1:
        raise RuntimeError(
            f"Expected exactly one JS320 with readable firmware information; found {len(devices)}. "
            "Disconnect extra Joulescopes and close other applications using the device."
        )
    return devices[0]


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def download_firmware(destination: Path) -> None:
    print(f"Downloading JS320 firmware {FIRMWARE_VERSION} from {FIRMWARE_URL}")
    with urlopen(FIRMWARE_URL, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("Firmware download was empty.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the serial-number confirmation prompt",
    )
    args = parser.parse_args()

    try:
        root = repository_root()
        python = venv_python(root)
        print("Checking the connected Joulescope. Stop active CMS runs first.")
        serial, current_version = run_info(root, python)
        print(f"Detected JS320 {serial} with firmware {current_version}.")

        current = version_tuple(current_version)
        target = version_tuple(FIRMWARE_VERSION)
        if current == target:
            print(f"JS320 {serial} already has firmware {FIRMWARE_VERSION}; no update needed.")
            return 0
        if current > target:
            print(
                f"JS320 {serial} has newer firmware {current_version}; "
                f"will not downgrade it to {FIRMWARE_VERSION}."
            )
            return 0

        if not args.yes:
            print("This will write firmware to the connected JS320. Close other apps using it.")
            confirmation = input(f"Type {serial} to confirm the firmware update: ").strip()
            if confirmation != serial:
                print("Confirmation did not match; cancelled.")
                return 1

        with tempfile.TemporaryDirectory(prefix="js320-firmware-") as temp_dir:
            package = Path(temp_dir) / "js320-1_1_9.zip"
            download_firmware(package)
            print(f"Programming JS320 {serial} with firmware {FIRMWARE_VERSION}.")
            programmed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pyjoulescope_driver",
                    "program",
                    "--package",
                    str(package),
                ],
                cwd=root,
                check=False,
            )
            if programmed.returncode:
                raise RuntimeError(
                    f"Firmware programming failed with exit code {programmed.returncode}."
                )

        print("Verifying firmware version.")
        verified_serial, verified_version = run_info(root, python)
        if verified_serial != serial or verified_version != FIRMWARE_VERSION:
            raise RuntimeError(
                f"Firmware verification failed: JS320 {verified_serial} reports "
                f"{verified_version}; expected JS320 {serial} at {FIRMWARE_VERSION}."
            )
        print(f"JS320 {serial} firmware update to {FIRMWARE_VERSION} verified.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
