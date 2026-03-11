@echo off
setlocal enabledelayedexpansion

if "%~1"=="" goto help

set "cmd=%~1"
set "arg=%~2"

if /i "%cmd%"=="build" goto build
if /i "%cmd%"=="clean-fw" goto clean_fw
if /i "%cmd%"=="clean-configs" goto clean_configs
if /i "%cmd%"=="gen-configs" goto gen_configs
if /i "%cmd%"=="test-smoke" goto test_smoke
if /i "%cmd%"=="test-ble" goto test_ble
if /i "%cmd%"=="test-ble-all" goto test_ble_all

echo Unknown command: %cmd%
goto help

:build
python -m pipenv run python .\apps\app_manager.py clean
if "%arg%"=="" (
  python -m pipenv run python .\apps\app_manager.py rebuild --board brd4182a
) else (
  python -m pipenv run python .\apps\app_manager.py rebuild --board %arg%
)
goto :eof

:clean_fw
python -m pipenv run python .\apps\app_manager.py clean
goto :eof

:clean_configs
python -m pipenv run python .\tests\configs\config_manager.py clean
goto :eof

:gen_configs
if "%arg%"=="" (
  python -m pipenv run python .\tests\configs\config_manager.py generate --mode both
) else (
  python -m pipenv run python .\tests\configs\config_manager.py generate --mode %arg%
)
goto :eof

:test_smoke
python -m pipenv run pytest .\tests\joulescope_interface.py -o log_cli=true --log-cli-level=INFO
goto :eof

:test_ble
if "%arg%"=="" (
  echo Usage: make test-ble ^<case^>
  exit /b 1
)
python -m pipenv run pytest .\tests\ble.py::test_%arg% -o log_cli=true --log-cli-level=INFO
goto :eof

:test_ble_all
python -m pipenv run pytest .\tests\ble.py -o log_cli=true --log-cli-level=INFO
goto :eof

:help
echo Available commands:
 echo   make build [board]
 echo   make clean-fw
 echo   make clean-configs
 echo   make gen-configs [one_factor^|full_sweep^|both]
 echo   make test-smoke
 echo   make test-ble ^<case^>
 echo   make test-ble-all
echo.
echo Examples:
 echo   make build brd4182a
 echo   make clean-fw
 echo   make clean-configs
 echo   make gen-configs full_sweep
 echo   make test-ble bgapi
 echo   make test-ble-all

endlocal
