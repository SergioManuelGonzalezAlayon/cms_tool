# Current Measurement

The Current Measurement tool is intended to measure the current consumption of the Silabs EFR32 devices
so that different wireless stacks can be measured and further optimized in power consumption for the end
products. Since this is a Silabs tool is should be integrated with the UTF since it is integrated into 
various SW Development teams, mainly the SW SQA team. 

## Test structure overview

The main test logic lives in `tests/ble.py` and is organized around:

- **Configuration**
  The configuration of the features is done inside the test as `<Device><Feature>Config` so that these are 
  read from an specific of default configuration file under `tests/configs/<protocol>/<test_name>` so that
  we know the exact variables of the test that affect the BLE Event. 

Currently the Beacon, Peripheral and Central Connection cases are covered, different test results will be acquired from different configurations. The PAwR test are not yet implemented.

## Usage

The goal of this tool is to get the current estiomation data from N amount of BLE Events, which are defined as the 
interval between two consecutives WakeUp from Deep Sleep, which include the processing, transmission and return
to Deep Sleep. There are two main uses, the developer investigating power consumption will require local feedback
on his desk so that the feedback loop is closed faster, and for the Jenkins agent running the tests on a the lab 
testing bench that will be inside a Ramsay box.

### For the developer

Most environments are `make` friendly and thus we have also added `just` to add more variety:

Examples:
```powershell
# Run a single BLE test
just/make test-ble beacon

# Run all BLE tests
just/make test-ble-all

However, the tests are run within a virtual python environment inside `pipenv` and can be called by using `pytest` as
intended. The only requirement is that the devices will be flashed with the applications on this repository using
the `slc` and `cmake` tools, that way the system can test the application correctly.
