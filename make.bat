@echo off
setlocal enabledelayedexpansion

if "%~1"=="" goto help

set "cmd=%~1"
set "arg=%~2"

if /i "%cmd%"=="test-ble" goto test_ble
if /i "%cmd%"=="test-ble-all" goto test_ble_all

echo Unknown command: %cmd%
goto help

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
 echo   make test-smoke
 echo   make test-ble ^<case^>
 echo   make test-ble-all
 echo.
echo Examples:
 echo   make test-ble bgapi
 echo   make test-ble-all

endlocal
