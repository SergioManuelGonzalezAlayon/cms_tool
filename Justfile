set shell := ["powershell", "-NoProfile", "-Command"]

default:
    just --list

test-smoke:
    python -m pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO


# Run a single BLE test with live logging
test-ble case:
    python -m pipenv run pytest .\tests\ble.py::test_{{case}} -o log_cli=true --log-cli-level=INFO

# Run all BLE tests with logging
test-ble-all:
    python -m pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO

# Override COM port at call time
test-ble-com port:
    python -m pipenv run pytest .\tests\ble.py::test_bgapi -o log_cli=true --log-cli-level=INFO
