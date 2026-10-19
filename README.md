# Current Measurement

Current Measurement is a hardware-backed test tool for measuring power consumption on Silicon Labs EFR32 devices across BLE scenarios. The repository contains firmware build helpers, BLE test cases, Joulescope capture utilities, and config-generation tooling for parameter sweeps.

## Current Scope

The main BLE test logic lives in `tests/ble.py`.

Implemented and wired into the current workflow:

- `legacy_beacon`
- `legacy_conn_adv`
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

## Common Workflows

Every pytest run now writes a JUnit XML result file to `artifacts/pytest-results.xml`, which is useful for CI test reporting and local run inspection.

### Building the applications

There are multiple applications to test a wireless stack, we have chosen an NCP App in our testing system to easy not only development but to simplify having to use use too many applications and potentially wearing the flash of the DUT as we run multiple test batches.

The first suggested step is to clean the previously build applications, the test system wont flash previously used aplications but this will generate test results based on previous firmware, which is not desired.

```powershell
uv run python .\apps\app_manager.py clean
```

Then using the `app_manager.py` you can clean, generate, build and rebuild applications. The sensible way would be to clean the workspace, once created the projects are generated under `.fw_build` and you can build said applications. The application details are defined in `dev_config.json`

```powershell
uv run python .\apps\app_manager.py clean
uv run python .\apps\app_manager.py generate
uv run python .\apps\app_manager.py build
```

The `rebuild` option is intented to allow for iterative development of an application that has potentially had any changes on the SLC configuration SiSDK that has been chosen. This is intended to be used to custom changes to an application.

```powershell
uv run python .\apps\app_manager.py rebuild
```

Future work needs to address building applications for protocol specific tests, since not all applications or devices support the same technologies or features.


### Test Configs

The CMS uses test configurations in a file to be able to trace back the actual paramters in a test that has been run. There are `default` configurations that are generic options for the protocol in question that allow for a quick test iteration. 

For more detailed testing the `config_manager.py` is used to generate two types of configs:

* `one_factor`: One variable is modified at a time, keeping all other settings at their default values.
* `full_sweep`: Configurations are varied across multiple parameters allowing to generate a greater result generation, that can be used to extrapolate other power consumption values.

#### Generating Test Configs

Generate both one-factor and full-sweep matrices:

```powershell
uv run python .\tests\configs\config_manager.py generate --mode both
```

Generate only one subset:

```powershell
uv run python .\tests\configs\config_manager.py generate --mode one_factor
uv run python .\tests\configs\config_manager.py generate --mode full_sweep
```

### Running tests

In order to run a test there first has to be the corresponding test config, in the repo there are default configurations that allow to run a generic configuration to obtain results. For the sake of simplicity these examples assume that you want to run the BLE Beacon test, where the DUT uses non-connectable Extended Advertising to broadcast a payload:

```powershell
uv run python .\test_orchestrator.py .\tests\configs\ble\beacon --mode default --feature beacon
```

There is another option for test running that includes a generated set of tests that cover multiple ranges of variables without going into a deep variable sweep, but these are generated by the `config_manager.py` and have to be created first.

```powershell
uv run python .\tests\configs\config_manager.py generate --mode one_factor
uv run python .\test_orchestrator.py .\tests\configs\ble\beacon --mode one_factor --feature beacon
```

Finally, the full sweep mode is intended to generate within all the variables for the test cases multiple points called `steps`, this will create a considerable set of tests that will test all the features.

```powershell
uv run python .\tests\configs\config_manager.py generate --mode full_sweep
uv run python .\test_orchestrator.py .\tests\configs\ble\beacon --mode full_sweep
```

### Pre-push unit tests

The repository includes a pre-push hook at `.githooks/pre-push`. Enable it for this checkout with:

```powershell
git config core.hooksPath .githooks
```

Before each push, the hook runs `uv run pytest utest` from the repository root and blocks the push if the suite fails. Run the same command manually at any time.

## Notes On Current Status

The repository has been migrated to `uv` structurally, but environment validation is still machine-dependent.

Known caveat:

- `pyjoulescope-driver` may require native compilation on Windows
- if that happens, a missing MSVC / Windows SDK install can block `uv sync`

If `uv sync` or pytest collection fails while building Joulescope dependencies, verify the local Visual C++ build tools and Windows SDK installation first.

