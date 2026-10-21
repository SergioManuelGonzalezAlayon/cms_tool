# Current Measurement System

Current Measurement System (CMS) Tool coordinates firmware builds and current-consumption measurements for Silicon Labs devices. It's the main data
generation method for the Battery Estimator tool and aimed to aid the current
measurement between different firmware images.

## Start with the CMS guides

The guides below are the operational documentation. Use this README for orientation and the test entry points; use the guides for setup and full workflows.

| Guide | Use it for |
| --- | --- |
| [CMS Client](docs/cms_client.md) | Select a bench, build and stage firmware, run or monitor measurements, and download artifacts. |
| [CMS Bench](docs/cms_bench.md) | Configure the hardware-facing bench, understand measurement execution, and inspect results and artifacts. |
| [CMS Daemon](docs/cms_daemon.md) | Start, inspect, stop, and restart the service running on a bench. |

## Requirements

- Python 3.10
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- [Just](https://just.systems/) for repository task recipes

Firmware builds and hardware-backed measurements also require the Silicon Labs tools, local configuration, and connected lab equipment described in the CMS guides.

Create the project environment with:

```powershell
uv sync
```

Local configuration is based on the `dev_config.json.template`, `cms_client_config.json.template`, and `cms_bench_runtime.json.template` files. Follow the relevant CMS guide for which configuration belongs on each machine.

## Run tests

Run the hardware-independent unit suite with the repository task in the [Justfile](Justfile):

```powershell
just test-unit
```

The `test-unit` recipe excludes `utest/test_s3_upload_live.py`, which performs a live data-lake upload. Pytest writes JUnit results to `artifacts/pytest-results.xml`. The portable collection and dry-run checks are defined in the [GitHub Actions workflow](.github/workflows/container-portable-checks.yml).

Actual firmware builds and measurements use the CMS Client and a configured bench. Start with the [CMS Client guide](docs/cms_client.md); its bench prerequisites are covered by the [CMS Bench guide](docs/cms_bench.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `apps/` | Silicon Labs application projects and the firmware build manager. |
| `tests/` | Hardware-backed test scenarios, fixtures, and test configurations. |
| `utest/` | Unit tests for orchestration, CMS components, and utilities. |
| `docs/` | CMS usage guides and architecture documentation. |
| `scripts/` | Container and repository helper scripts. |
| `utils/` | Shared runtime utilities. |
| `container/` | Container build inputs and bundled vendor-tool location. |
| `.github/workflows/` | GitHub Actions workflows. |
| `artifacts/` | Generated test reports and measurement artifacts. |
| `.fw_build/` | Generated firmware build workspace. |

The main CMS entry points are `cms_client.py` (client CLI), `cmsd.py` (bench daemon), `cms_bench.py` (local bench execution), and `cms_protocol.py` (shared request and result models). `test_orchestrator.py` runs the test campaigns used by the bench.

## GitHub Actions

The current portable workflow is [container-portable-checks.yml](.github/workflows/container-portable-checks.yml). It builds the container, collects BLE tests, and runs an orchestrator dry run; hardware-backed measurements require a configured lab bench.
