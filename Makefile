.PHONY: default help build clean-fw clean-fw-dut clean-fw-tester clean-configs gen-configs test-smoke test-ble test-ble-all test-ble-beacon test-ble-connadv-central test-ble-connadv-peripheral

default: help

help:
	@echo "Available targets:"
	@echo ""
	@echo "  make build [board=<name>]        Rebuild all apps under apps into .fw_build"
	@echo "  make clean-fw                    Remove all generated firmware under .fw_build"
	@echo "  make clean-fw-dut                Remove the generated DUT project under .fw_build\\dut"
	@echo "  make clean-fw-tester             Remove the generated tester project under .fw_build\\tester"
	@echo "  make clean-configs               Remove generated test config JSONs under tests\\configs"
	@echo "  make gen-configs                 Generate all config matrices, including full sweeps"
	@echo ""
	@echo "  make test-smoke                  Run the Joulescope smoke test suite"
	@echo "  make test-ble case=<name>        Run a single BLE test (e.g. case=beacon)"
	@echo "  make test-ble-all                Run all BLE tests"
	@echo ""
	@echo "Examples:"
	@echo "  make build board=brd4182a"
	@echo "  make clean-fw"
	@echo "  make clean-configs"
	@echo "  make gen-configs"
	@echo "  make test-ble case=beacon"
	@echo "  make test-ble-all"

build:
	python -m pipenv run python .\apps\app_manager.py clean
	python -m pipenv run python .\apps\app_manager.py rebuild --board $(if $(board),$(board),brd4182a)

clean-fw:
	python -m pipenv run python .\apps\app_manager.py clean

clean-fw-dut:
	@powershell -NoProfile -Command "if (Test-Path '.fw_build\\dut') { Remove-Item -Recurse -Force '.fw_build\\dut' }"

clean-fw-tester:
	@powershell -NoProfile -Command "if (Test-Path '.fw_build\\tester') { Remove-Item -Recurse -Force '.fw_build\\tester' }"

clean-configs:
	python -m pipenv run python .\tests\configs\config_manager.py clean

gen-configs:
	python -m pipenv run python .\tests\configs\config_manager.py generate --mode $(if $(mode),$(mode),both)

test-smoke:
	python -m pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO

test-ble:
	python -m pipenv run pytest .\tests\ble.py::test_$(case) -o log_cli=true --log-cli-level=INFO

test-ble-all:
	python -m pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
