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

This repository no longer depends on `just`, `make`, or `make.bat` wrappers for normal workflows. Use the direct `uv`, Python, and pytest commands shown below.

## Which Tool To Use

Use the repo tools by responsibility:

- `uv sync` installs and locks the Python environment for this repository
- `uv run pytest ...` is the supported way to run tests
- `uv run python .\tests\configs\config_manager.py ...` manages generated test configs
- `uv run python .\test_orchestrator.py ...` runs multi-config test campaigns
- `uv run python .\apps\app_manager.py ...` manages `.slcp` project generation, upgrade, and firmware builds

In other words:

- use `pytest` when you want to run one test or one test file
- use `config_manager.py` when you want to generate or clean config matrices
- use `test_orchestrator.py` when you want to execute many configs as a campaign
- use `app_manager.py` when you want to generate, upgrade, rebuild, or clean firmware outputs

## SDK And Toolchain

The Silicon Labs SDK is not installed or version-managed by this repository itself.

What the repo expects from the host machine:

- Simplicity Studio command-line tools on `PATH`
- `slc` for `.slcp` project generation and upgrade
- `cmake` for firmware builds after project generation
- `commander` for probe interaction, flashing, and adapter voltage setup
- a valid BGAPI XAPI file path for `sl_bt.xapi`

How the repo uses those tools:

- `apps/app_manager.py` drives `slc generate`, `slc upgrade`, and `cmake --workflow --preset project`
- pytest fixtures in `tests/conftest.py` use `commander` to set adapter voltage and flash prebuilt images when required
- BGAPI device creation in `utils/bgapi_utils.py` reads `ble.xapi` from `dev_config.json`, or falls back to `BGAPI_XAPI`

What you need to configure locally:

- copy `dev_config.json.template` to `dev_config.json`
- set `ble.xapi` to your host's `sl_bt.xapi`
- if the CLI tools are not already on `PATH`, set `tools.slc`, `tools.commander`, and optionally `tools.cmake`
- set DUT/tester serial ports and probe serial numbers
- optionally set `firmware.build_root`, `firmware.board`, and `firmware.bootloader_slcp`

## Container Workflow

Container support is now available as a phase 1 additive workflow for CI and portable lab hosts. The planning document lives in `CONTAINER.md`.

Current status:

- the repository now ships a root `Dockerfile` and `.dockerignore`
- phase 1 is focused on a portable repo runtime with the full lab toolchain installed in the image
- the image keeps its virtual environment outside the mounted repo so bind mounts do not hide the installed dependencies
- `scripts/run_lab_container.sh` provides a Linux lab-host and BeagleBone Black friendly launcher
- the native `uv` workflow described above remains supported
- multi-arch publishing is still a future phase

### Build The Container Locally

Then build the image:

```bash
docker build -t current-measurement .
podman build -t current-measurement .
```

The image installs the generic build toolchain itself:

- `uv`
- `cmake`
- `ninja-build`
- `build-essential`

Put the Silicon Labs payload under `container/vendor-tools/` before building. The `Dockerfile` copies that directory directly into `/opt/silabs`, and the image `PATH` includes `/opt/silabs/bin`.

Expected tool bundle layout:

```text
container/vendor-tools/
  bin/
    commander
    slc
  sdk/
    ...
```

Run the default portable smoke check from the image:

```bash
docker run --rm -it current-measurement
```

Run the same image against your checked-out repo:

```bash
docker run --rm -it -v "${PWD}:/workspace" current-measurement
```

Run a direct command in the container:

```bash
docker run --rm -it -v "${PWD}:/workspace" current-measurement \
  python tests/configs/config_manager.py generate --mode both
```

```bash
docker run --rm -it -v "${PWD}:/workspace" current-measurement \
  python -m pytest tests/joulescope_interface.py --collect-only
```

The intended usage model is:

- use the container for portable Python setup, config generation, pytest entrypoints, and report generation
- use GitHub Actions with the container for portable checks that do not require attached hardware
- use the same container on self-hosted runners, Raspberry Pi, or BeagleBone Black class hosts for hardware-backed runs

For hardware-backed execution, the image should contain the repo toolchain, including:

- Python dependencies
- `cmake`
- `ninja-build`
- `slc`
- `commander`
- any SDK assets the workflow expects inside the container

The host should still provide:

- connected Joulescope hardware
- serial access to DUT and tester devices
- `dev_config.json`, either in the repo root or through `CURRENT_MEASUREMENT_DEV_CONFIG_FILE`
- any USB or device permissions required to reach the attached lab hardware

### Linux Lab Host / BeagleBone Black Flow

For a Linux lab host, including a BeagleBone Black, use the launcher script:

```bash
bash scripts/run_lab_container.sh
```

That default command runs the beacon orchestrator against the `default` config:

```bash
python test_orchestrator.py tests/configs/ble/beacon --mode default --feature beacon --max-fail 1
```

You can pass any container command through the launcher:

```bash
bash scripts/run_lab_container.sh python test_orchestrator.py tests/configs/ble/beacon --generate --mode one_factor --feature beacon --max-fail 1
```

Useful environment variables for lab hosts:

- `CURRENT_MEASUREMENT_IMAGE=current-measurement` picks the image tag to run
- `CURRENT_MEASUREMENT_CONTAINER_ENGINE=docker|podman` selects the container engine explicitly
- `CURRENT_MEASUREMENT_DEV_CONFIG_FILE=/path/to/dev_config.json` mounts a config file without copying it into the repo
- `BGAPI_XAPI=/opt/silabs/sdk/protocol/bluetooth/api/sl_bt.xapi` points the test runtime at a bundled XAPI when you do not want that path in `dev_config.json`

Example BeagleBone Black run using an external config file:

```bash
CURRENT_MEASUREMENT_DEV_CONFIG_FILE=/etc/current-measurement/dev_config.json \
bash scripts/run_lab_container.sh python test_orchestrator.py tests/configs/ble/beacon --generate --mode one_factor --feature beacon --max-fail 1
```

The phase 1 image now covers the portable runtime contract, a lab-host launcher, and a portable GitHub Actions check. Treat `CONTAINER.md` as the source of truth for the remaining rollout and multi-arch work.

## Common Workflows

Every pytest run now writes a JUnit XML result file to `artifacts/pytest-results.xml`, which is useful for CI test reporting and local run inspection.

### Check Pytest Collection

Use this first when validating that the Python environment is healthy:

```powershell
uv run pytest --collect-only .\tests\ble.py
```

### Build Firmware Projects

```powershell
uv run python .\apps\app_manager.py rebuild --board brd4182a
```

### Clean Generated Firmware

```powershell
uv run python .\apps\app_manager.py clean
```

### Clean Generated Test Configs

```powershell
uv run python .\tests\configs\config_manager.py clean
```

### Generate Test Configs

Generate both one-factor and full-sweep matrices:

```powershell
uv run python .\tests\configs\config_manager.py generate --mode both
```

Generate only one subset:

```powershell
uv run python .\tests\configs\config_manager.py generate --mode one_factor
uv run python .\tests\configs\config_manager.py generate --mode full_sweep
```

### Run Joulescope Smoke Tests

```powershell
uv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO
```

### Run BLE Tests

Run a single BLE case:

```powershell
uv run pytest .\tests\ble.py::test_beacon --cfg default.json -o log_cli=true --log-cli-level=INFO
```

Run a single BLE case with a specific config file:

```powershell
uv run pytest .\tests\ble.py::test_beacon --cfg non_conn_adv_300ms.json -o log_cli=true --log-cli-level=INFO
```

Run all BLE tests:

```powershell
uv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
```

### Run A Test Campaign

Generate configs, then run a feature or config batch through the orchestrator:

```powershell
uv run python .\tests\configs\config_manager.py generate --mode both
uv run python .\test_orchestrator.py .\tests\configs\ble\beacon --mode one_factor --feature beacon --max-fail 1
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
