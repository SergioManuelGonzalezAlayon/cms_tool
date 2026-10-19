set shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

default:
    @just --list

# Run the hardware-independent unit test suite.
test-unit:
    uv run pytest utest

# Enable the repository's tracked Git hooks for this checkout.
install-hooks:
    git config core.hooksPath .githooks

# Build, stage, and run normal then differential beacon measurements through
# the configured CMS bench. Each command is a PowerShell invocation.
run-test-normal:
    uv run python .\cms_client.py health
    uv run python .\cms_client.py BUILD
    uv run python .\cms_client.py SEND_FW
    uv run python .\cms_client.py TEST --mode default --feature beacon --verbose

run-test-custom:
    uv run python .\cms_client.py health
    uv run python .\cms_client.py BUILD
    uv run python .\cms_client.py SEND_FW
    uv run python .\cms_client.py TEST --mode default --feature beacon --config-name custom --verbose
    
run-test-diff:
    uv run python .\cms_client.py health    
    uv run python .\cms_client.py BUILD --diff
    uv run python .\cms_client.py SEND_FW --diff
    uv run python .\cms_client.py TEST --mode diff --feature beacon --verbose
