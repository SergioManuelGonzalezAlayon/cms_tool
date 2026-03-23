# Current Measurement

Current Measurement is a hardware-backed test tool for measuring power consumption on Silicon Labs EFR32 devices across BLE scenarios. The repository contains firmware build helpers, BLE test cases, Joulescope capture utilities, and config-generation tooling for parameter sweeps.

## Current Scope

The main BLE test logic lives in `tests/ble.py`.

Implemented and wired into the current workflow:

- `beacon`
- `conn_adv`
- `conn_central`
- `conn_peripheral`
- `scanner`
- `PAwR_obs`

## Prerequisites

- Python `3.10`
- `uv`
- Windows machine with the required hardware tools on `PATH` for firmware workflows:
  - `slc`
  - `cmake`
  - `commander`
- Joulescope-connected test setup for hardware-backed capture and BLE power tests

For some environments, installing `pyjoulescope-driver` may also require a working Visual C++ / Windows SDK toolchain.

## Setup

This repository uses `uv` for dependency management and command execution.

```powershell
uv sync
```

If you want to include development dependencies when they are added later:

```powershell
uv sync --dev
```

## Common Workflows

### Build Firmware Projects

```powershell
just build
make build
uv run python .\apps\app_manager.py rebuild --board brd4182a
```

### Clean Generated Firmware

```powershell
just clean-fw
make clean-fw
```

### Clean Generated Test Configs

```powershell
just clean-configs
make clean-configs
uv run python .\tests\configs\config_manager.py clean
```

### Generate Test Configs

Generate both one-factor and full-sweep matrices:

```powershell
just gen-configs
make gen-configs
uv run python .\tests\configs\config_manager.py generate --mode both
```

Generate only one subset:

```powershell
just gen-configs one_factor
uv run python .\tests\configs\config_manager.py generate --mode one_factor
uv run python .\tests\configs\config_manager.py generate --mode full_sweep
```

### Run Joulescope Smoke Tests

```powershell
just test-smoke
make test-smoke
uv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO
```

### Run BLE Tests

Run a single BLE case:

```powershell
just test-ble beacon
make test-ble case=beacon
uv run pytest .\tests\ble.py::test_beacon --cfg default.json -o log_cli=true --log-cli-level=INFO
```

Run a single BLE case with a specific config file:

```powershell
just test-ble beacon non_conn_adv_300ms.json
uv run pytest .\tests\ble.py::test_beacon --cfg non_conn_adv_300ms.json -o log_cli=true --log-cli-level=INFO
```

Run all BLE tests:

```powershell
just test-ble-all
make test-ble-all
uv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
```

## Config Layout

Generated test configurations live under `tests/configs`. The config manager supports:

- `generate`
- `clean`
- `rebuild`
- `--mode one_factor|full_sweep|both`

## Notes On Current Status

The repository has been migrated to `uv` structurally, but environment validation is still machine-dependent.

Known caveat:

- `pyjoulescope-driver` may require native compilation on Windows
- if that happens, a missing MSVC / Windows SDK install can block `uv sync`

If `uv sync` or pytest collection fails while building Joulescope dependencies, verify the local Visual C++ build tools and Windows SDK installation first.
