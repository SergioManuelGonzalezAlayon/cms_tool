set shell := ["powershell", "-NoProfile", "-Command"]

default:
    just --list

build board="brd4182a":
    pipenv run python .\apps\app_manager.py clean
    pipenv run python .\apps\app_manager.py rebuild --board {{board}}

clean-fw:
    pipenv run python .\apps\app_manager.py clean

clean-fw-dut:
    if (Test-Path .fw_build\dut) { Remove-Item -Recurse -Force .fw_build\dut }

clean-fw-tester:
    if (Test-Path .fw_build\tester) { Remove-Item -Recurse -Force .fw_build\tester }

clean-configs:
    pipenv run python .\tests\configs\config_manager.py clean

gen-configs mode="both":
    pipenv run python .\tests\configs\config_manager.py generate --mode {{mode}}

test-smoke:
    pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO

# Run a single BLE test with live logging
# Usage: just test-ble beacon non_conn_adv_300ms.json
test-ble case cfg="default.json":
    pipenv run pytest .\tests\ble.py::test_{{case}} --cfg {{cfg}} -o log_cli=true --log-cli-level=INFO

# Test Joulescope for debug purposes
test-js case:
    pipenv run pytest .\tests\joulescope_interface.py::test_{{case}} -o log_cli=true --log-cli-level=INFO

# Run all BLE tests with logging
test-ble-all:
    pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
