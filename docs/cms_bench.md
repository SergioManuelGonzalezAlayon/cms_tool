# CMS Bench

The CMS Bench is the hardware-facing execution side of the Current Measurement
System. In the v2 architecture, the bench owns local measurement authority:
physical device access, firmware validation, test execution, signal capture, and
artifact collection happen on the machine connected to the DUT, tester, and
measurement equipment.

The bench is intentionally not the CMS Client. The client builds firmware,
selects tests, coordinates workflow, and submits measurement requests. The bench
receives staged firmware and metadata from the daemon layer, validates that the
request matches the local hardware, runs one measurement at a time, and returns a
structured result with artifacts.

## Architecture Position

`docs/cms_tool.md` and `docs/newCMSArch.html` define the split between
orchestration and physical execution:

| Area | Owner |
| --- | --- |
| Build orchestration | CMS Client |
| Test selection and workflow | CMS Client |
| Bench reservation and scheduling | HWMux direction / integration layer |,
| HTTP/session handling | CMS Bench daemon |
| Hardware readiness and local execution | CMS Bench |
| Signal capture and result evidence | CMS Bench |
| Historical storage and comparison | CMS Database / result workflow |

In code, `cms_bench.py` implements the local execution primitives. It is
transport-agnostic: it does not expose HTTP routes, own remote client policy, or
perform bench scheduling. That boundary belongs above it, currently to the bench
daemon layer. `CMSBench` is the object that controls the local bench resources
for one active measurement.

## What The Bench Does

The bench performs the parts of a current-measurement run that must happen close
to the physical setup:

1. Load persistent bench identity and hardware mappings from
   `cms_bench_runtime.json`.
2. Report bench health, including board/device configuration, DUT and tester
   probe mappings, serial-port mappings, Joulescope mapping, Commander
   availability, XAPI availability, and current bench state.
3. Validate staged firmware and metadata for one measurement request.
4. Confirm that the firmware board and device match the physical bench.
5. Verify staged image files and SHA-256 hashes for DUT, tester and required
6. `sl_bt.xapi`.
7. Prepare a pytest/test-orchestrator runtime layout from the staged firmware.
8. Write `cms_fw_metadata.json`, which is consumed by the test orchestration
   flow.
9. Execute `test_orchestrator.py` against the selected test configuration.
10. Capture stdout, stderr, pytest logs, orchestrator result logs, and
   diagnostic metadata.
1.  Collect result artifacts from the generated artifact directories and attach
    them to the structured measurement result.
2.  Return the bench to `idle` when the run completes or fails.

The bench state model is deliberately small: `offline`, `idle`, `busy`,
`testing`, and `error`. A healthy initialized bench becomes `idle`; an active
measurement moves through `testing`; health reporting exposes `busy` when the
bench is not available for another run.

## Runtime Configuration

`cms_bench_runtime.json` is persistent, local bench configuration. It identifies
the physical bench and maps the host-visible resources needed to run tests:

The daemon bounds each orchestrator execution to five minutes by default. Set
`CMSD_EXECUTION_TIMEOUT_S` when starting `cmsd.py` to allow a longer campaign;
for example, use `CMSD_EXECUTION_TIMEOUT_S=3600` for a one-hour limit. A timeout
terminates the managed process tree and records `process_timed_out` in the
measurement's RAM-backed event stream.

Cancellation is cooperative at the daemon boundary and forceful at the process
boundary. The cancel route first records `cancellation requested`; the active
orchestrator process group then receives `SIGTERM`, followed by `SIGKILL` after
the bounded grace period if necessary. The daemon publishes the terminal
`cancelled` result only after stdout and stderr have been drained and the worker
has stopped.

| Section | Purpose |
| --- | --- |
| `bench` | Stable bench identity, such as `bench_id` and `profile` |
| `devices` | Physical board, board revision, and device identity |
| `dut` | DUT serial port and debug probe serial number |
| `tester` | Tester serial port and debug probe serial number |
| `tools` | Local tool paths, especially Simplicity Commander |
| `ble` | Optional local fallback path for `sl_bt.xapi` |
| `joulescope` / `Joulescope` | Joulescope serial, port, or ID mapping |

Current required fields for initialization are:

- `bench.bench_id`
- `devices.board`
- `devices.device`
- `dut.probe_sn`
- `tester.probe_sn`
- `dut.serial_port`
- `tester.serial_port`
- `tools.commander`

The Joulescope mapping is health-checked as optional in the current bench
implementation, so a missing mapping reports a warning rather than a startup
failure. The current loader recognizes `joulescope` or `Joulescope` keys with
`serial_number`, `serial_port`, or `id`. If the runtime file uses a different
shape, such as `js.usb_id`, the bench will not populate
`resources.joulescope_serial` until the configuration shape or loader is
updated.

## Measurement Inputs

A remote measurement is represented by a `MeasurementRequest` prepared by the
daemon and passed into `CMSBench` as a `BenchSession`. The session contains:

| Field | Meaning |
| --- | --- |
| `measurement_id` | Unique run identifier |
| `runtime_dir` | Directory where the daemon staged firmware and where bench diagnostic logs are written |
| `manifest` | Firmware assets staged for execution |
| `request` | Test selector, measurement mode, metadata, and upload preference |
| `notes` | Non-fatal validation notes collected during preparation |

Firmware inputs include:

- DUT image
- Tester image
- Optional bootloader image
- Required `sl_bt.xapi`
- Board and device metadata
- SiSDK version and commit metadata
- Git commit metadata
- Optional SHA-256 hashes for validation

The test selector chooses:

- `selector.mode`: top-level test suite/config family, for example `ble`
- `selector.feature`: feature directory under `tests/configs/<mode>/`
- `selector.config`: JSON config name, defaulting to `default`
- `request.mode`: measurement profile, such as `default` or `full_sweep`

## Result Upload Policy

CMS Bench result upload is mode-based:

- `default`, `full_sweep`, and `diff` runs upload results to the configured
  database by default.
- `debug` runs do not upload results to the database.

The daemon enforces this policy before creating the `BenchSession`, and
`cms_bench.py` forwards `--upload-results-to-database` to `test_orchestrator.py`
only when the normalized request flag is true. This keeps debug runs local even
if a request contains an unexpected upload value, and keeps normal bench runs
database-backed even when the request omits the flag.

## Execution Flow

The current `measurement_run()` flow is:

1. Mark the bench as `testing`.
2. Run `test_smoke()`, which checks initialization, runnable state, and firmware
   preparation.
3. Validate firmware files and hashes.
4. Reject board/device mismatches between the request and the bench.
5. Require a client-supplied `sl_bt.xapi`.
6. Create a per-run `fw_build` layout under the session runtime directory.
7. Copy DUT and tester images into the expected project build locations.
8. Copy the optional bootloader image when provided.
9. Generate `cms_fw_metadata.json`.
10. Resolve the requested config file from
    `tests/configs/<selector.mode>/<selector.feature>/<selector.config>.json`.
11. Run:

```text
python test_orchestrator.py <config_dir> --mode <default|full_sweep> --feature <feature> --results-log <runtime_dir>/orchestrator_results.jsonl --cms-runtime-config cms_bench_runtime.json --cms-fw-metadata <runtime_dir>/cms_fw_metadata.json
```

12. Add `--cfg <selector.config>` when the selected config is not `default`.
13. Add `--upload-results-to-database` for non-debug CMS Bench runs.
14. Write `test_stdout.log`, `test_stderr.log`, and `cms_test_run.json`.
15. Convert the orchestrator return code into `passed` or `failed`.
16. Collect diagnostic and test artifacts.
17. Mark progress as 100 percent and return the final `MeasurementResult`.
18. Return the bench state to `idle`.

For `request.mode == full_sweep`, the bench passes `full_sweep` to the
orchestrator. Other modes currently map to the orchestrator `default` mode.

## Artifacts

The bench returns a `MeasurementResult` containing the final state, timing,
progress, message, and artifact metadata.

Diagnostic artifacts are collected from the measurement runtime directory when
present:

- `cms_fw_metadata.json`
- `cms_test_run.json`
- `orchestrator_results.jsonl`
- `pytest.log`
- `test_stdout.log`
- `test_stderr.log`

Measurement artifacts are collected from:

- `artifacts/<selector.mode>/<feature>/<default|full_sweep>/`
- `artifacts/orchestrator/`

Only files modified after the run preparation timestamp are attached when that
timestamp is available. This keeps the returned artifact list focused on the
current measurement.

## Health Check Behavior

`health_check()` builds a `BenchHealthReport` from individual checks:

- bench startup and initialization
- configured board and device
- configured DUT probe and tester probe
- configured DUT and tester serial mappings
- optional Joulescope mapping
- Commander path resolution
- optional XAPI path resolution
- current bench state

Any failed required check makes the overall health state `error`. Warnings, such
as missing optional Joulescope or XAPI information, keep the bench discoverable
but visible as degraded. DUT and tester device states are considered ready only
when their role-specific checks pass.

## What The Bench Does Not Do

The bench layer does not:

- Build firmware from SLCP projects.
- Select test campaigns for the user.
- Own HTTP routes or remote authentication.
- Allocate shared lab resources.
- Provide raw remote control of instruments.
- Permanently store active jobs, staged firmware, captures, or generated
  artifacts.
- Own historical comparison policy.

Those responsibilities belong to the client, daemon, HWMux integration, database
workflow, or result-management layer depending on the architecture boundary.

## Compatibility Helpers

`cms_bench.py` still includes compatibility helpers for older local test flows:

- `CMSBenchRunRequest`
- `CMSBenchResultAnalysis`
- `CMSBench.init(dev_config, js=None)`
- `CMSBench.run_test(...)`
- `resolve_cms_resources_from_dev_config(...)`

These keep older local bench tests working while the remote client/daemon/bench
architecture continues to take shape. New remote execution should use the
`BenchSession` and `MeasurementRequest` flow.

## Design Rule

The core architecture rule from `newCMSArch.html` applies directly to this file:

> CMS Bench owns live measurement authority locally. CMS Client owns
> orchestration and workflow.

That rule is why `cms_bench.py` focuses on local resources, validation,
orchestrator execution, and artifacts, while leaving network policy and remote
workflow control outside the bench primitive.
