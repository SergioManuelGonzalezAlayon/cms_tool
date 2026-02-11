.PHONY: default help test-smoke test-ble test-ble-all test-ble-beacon test-ble-connadv-central test-ble-connadv-peripheral

default: help

help:
	@echo "Available targets:"
	@echo ""
	@echo "  make test-smoke                  Run the Joulescope smoke test suite"
	@echo "  make test-ble case=<name>        Run a single BLE test (e.g. case=beacon)"
	@echo "  make test-ble-all                Run all BLE tests"
	@echo ""
	@echo "Examples:"
	@echo "  make test-ble case=beacon"
	@echo "  make test-ble-all"

test-smoke:
	python -m pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO

test-ble:
	python -m pipenv run pytest .\tests\ble.py::test_$(case) -o log_cli=true --log-cli-level=INFO

test-ble-all:
	python -m pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
