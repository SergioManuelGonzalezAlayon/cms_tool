# Current Measurement

The Current Measurement tool is intended to measure the current consumption of the Silabs EFR32 devices
so that different wireless stacks can be measured and further optimized in power consumption for the end
products. Since this is a Silabs tool is should be integrated with the UTF since it is integrated into 
various SW Development teams, mainly the SW SQA team. 

## Test structure overview

The main test logic lives in `tests/ble.py` and is organized around:

- **Device fixtures**
  - `BLEDut`: initializes and validates the DUT
  - `BLETester`: initializes and validates the scanning device

- **Configuration fixtures**
  - `DUTAdvConfig`: creates and configures a BLE advertiser set on the DUT
  - `TesterScanConfig`: configures passive scanning parameters on the Tester

- **Scenario tests**
  - `test_bgapi`: basic BGAPI connectivity sanity check
  - `test_beacon`: non-connectable (beacon-style) advertising with a scanning observer

Additional tests for connectable advertising, scanning modes, and PAwR are scaffolded but not yet implemented.

## Usage

There are multiple flavors of automation for everyone, in here we support Justfiles by default but make and
bat options are also supported.

Examples:
```powershell
# Run a single BLE test
just/make test-ble bgapi

# Run all BLE tests
just/make test-ble-all