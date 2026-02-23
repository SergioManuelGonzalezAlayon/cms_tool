set shell := ["powershell", "-NoProfile", "-Command"]

default:
    just --list

test-smoke:
    pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO


# Run a single BLE test with live logging
test-ble case:
    pipenv run pytest .\tests\ble.py::test_{{case}} -o log_cli=true --log-cli-level=INFO

# Test Joulescope for debug purposes
test-js case:
    pipenv run pytest .\tests\joulescope_interface.py::test_{{case}} -o log_cli=true --log-cli-level=INFO

# Run all BLE tests with logging
test-ble-all:
    pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
