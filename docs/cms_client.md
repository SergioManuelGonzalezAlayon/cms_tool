# CMS Client

The CMS Client is the user-facing orchestration side of the Current Measurement
System. In the v2 architecture, the client owns workflow: building firmware,
preparing measurement metadata, talking to a CMS Bench daemon, starting remote
tests, polling progress, and downloading artifacts.

The client is intentionally not the CMS Bench. It does not flash devices,
control serial ports, drive the Joulescope, or perform live signal analysis.
Those operations remain local to the bench machine through `cmsd.py` and
`cms_bench.py`. The client sends requests and artifacts over the daemon
interface, then receives structured measurement results.

## Architecture Position

`docs/cms_tool.md` and `docs/newCMSArch.html` define the split between
orchestration and physical execution:

| Area | Owner |
| --- | --- |
| Build orchestration | CMS Client |
| Test selection and workflow | CMS Client |
| Developer and CI entry point | CMS Client |
| Bench reservation and scheduling | HWMux direction / integration layer |
| HTTP/session handling | CMS Bench daemon |
| Hardware readiness and local execution | CMS Bench |
| Signal capture and result evidence | CMS Bench |
| Historical storage and comparison | CMS Database / result workflow |

In code, `cms_client.py` implements the remote-control surface for one CMS Bench
daemon endpoint. It speaks the daemon protocol using JSON over HTTP, wraps the
local application build flow, prepares firmware payloads, and provides the
command-line entry point named `cms` by the parser.

## Selecting a Bench Endpoint

The default `daemon` entry in `cms_client_config.json` remains the endpoint when
no selector is supplied. The optional `benches` mapping names additional bench
endpoints; select one with:

```text
cms health --bench-id cms-bench-001
```

For a one-shot host/IP override that keeps the default daemon scheme, port,
token, and timeout, use:

```text
cms health --bench-ip 192.0.2.10
```

For a complete one-shot URL override, use:

```text
cms --base-url http://cms-bench-001:8000 health
```

Use exactly one endpoint selector per invocation: `--base-url`, `--bench-id`,
or `--bench-ip`. Combining them is rejected. `--api-token` may be used with any
one selector to override that invocation's configured token. `cms HEALTH`
prints the resolved endpoint before its health checks.

## Operator Quick Start

Use this sequence when you want to build firmware, stage it on the configured
CMS Bench, run one BLE measurement, reconnect if needed, cancel if needed, and
download artifacts without SSH.

This section is the short operator workflow. It is intentionally separate from
the architecture and protocol reference below, so a first-time operator can run
the normal path without reading the complete manual first.

1. Check daemon and bench health.

```text
cms health
```

This does not change the bench or any firmware. Continue when the summary is
not `error` and the required checks for daemon startup, runtime storage,
artifact storage, bench configuration, tools, board, device, and serial mappings
are reported as pass or expected warnings.

2. Check whether the bench is idle or already running a measurement.

```text
cms status
```

If a run is active, the output includes `active_measurement_id`.
This does not start or stop a test. Continue with a new run only when the bench
is idle, or use `cms WATCH` to attach to the active measurement.

3. List available BLE features and named configs.

```text
cms tests
```

This does not generate configs. It reads the bench-advertised persistent config
library. Continue when the feature you plan to run appears with the expected
config name, such as `default` or `dcdc`.

4. Build local firmware from the configured client build settings.

```text
cms BUILD
```

This changes only the local build output under the configured build root. It
does not stage firmware on the bench and does not start a measurement. Continue
when the build command exits successfully and writes current build provenance.

5. Stage the built firmware and build metadata on the bench.

```text
cms SEND_FW --request-id cms-dev-001 --requested-by <user>
```

This uploads or stages firmware and `sl_bt.xapi` for later use by the bench. It
does not run pytest or start a measurement. Continue when the response reports
that firmware is staged and ready for test.

6. Preview the default config that would be submitted.

```text
cms TEST --mode default --feature beacon --config-name default --preview
```

This validates the selected local config and prints the exact mode, feature,
config path, config hash, and result destination. It does not contact the daemon
and does not use the bench. Continue only when the displayed config is exactly
the one you intend to run.

For CMS Bench runs, result upload is selected by mode. `default`,
`full_sweep`, and `diff` tests upload results to the configured database by
default. `debug` tests do not upload results to the database.

7. Run the default config and watch it until completion.

```text
cms TEST --mode default --feature beacon --config-name default --out-dir artifacts
```

This submits a measurement using the most recently staged firmware, attaches to
the structured watch stream, and downloads final artifacts after completion. It
does not regenerate configs. A successful running submission prints the
measurement ID, current test, and total/pass/fail/remaining counts.

8. Run one exact named config and watch it until completion.

```text
cms TEST --mode default --feature beacon --config-name dcdc --out-dir artifacts
```

The client validates the selected config locally, sends its hash to the daemon,
and the bench validates the same config again before execution.
Continue only when the concise watch output shows the requested config identity
and either a passed terminal state or the failed identities you need to inspect.

9. Preview a variable-filtered generated campaign.

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview
```

This validates the filter against the selected feature's `parameters.json` and
prints the expected job count before bench time is spent. For `beacon`,
`parameters.json` declares the exact parameter name `phy` and accepted value
`2m`; use the spelling shown by preview.

10. Run the variable-filtered generated campaign and watch it until completion.

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --out-dir artifacts
```

This submits every generated `beacon` full-sweep case whose declared `phy`
parameter resolves to `2m`. It does not run the default config and it does not
select one exact config. The daemon and bench revalidate the filter and final
job count before hardware execution.

11. Reconnect to the active measurement from another shell or after a disconnect.

```text
cms WATCH
```

This only observes an already-running measurement. It does not restart, pause,
or cancel bench execution. Continue when the client prints the active
measurement summary and resumes counts from daemon state.

12. Reconnect to a known measurement ID from the beginning of retained output.

```text
cms WATCH <measurement_id> --cursor 0
```

This replays retained observation events for that measurement and refreshes the
structured summary. If older verbose console output has been truncated, the
client prints a truncation warning and continues from the oldest retained
cursor.

13. Reconnect after the last cursor you already saw.

```text
cms WATCH <measurement_id> --cursor <last_cursor>
```

This avoids duplicating already acknowledged events. Continue when the next
displayed events have cursors later than the cursor you supplied.

14. Show retained stdout, stderr, and lifecycle events while watching.

```text
cms WATCH <measurement_id> --verbose
```

This changes only the client display. It preserves the stdout/stderr source on
rendered console lines and still refreshes structured counts independently.

15. Force or disable colored human output when needed.

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --color always --out-dir artifacts
cms WATCH <measurement_id> --verbose --color never
```

The default is `--color auto`, which colors terminal output only when stdout is
a TTY. ANSI color is never written to JSON, JUnit XML, result artifacts, or
`cms FAILED --json`.

16. Cancel the active measurement.

```text
cms CANCEL
```

Cancellation requests the real bench-side process to stop; it is not just a
client display change.
Continue only when the measurement reaches a cancelled or other terminal state.

17. Download artifacts for a completed measurement by explicit ID.

```text
cms ARTIFACTS <measurement_id> --out-dir artifacts
```

This reads the published artifact list for the specified measurement and saves
those files locally. It does not use the daemon's `last` measurement shortcut.
Continue when every expected artifact path is printed or saved under the output
directory.

18. List failed test identities and rerun commands for a completed measurement.

```text
cms FAILED <measurement_id>
```

For machine-readable output:

```text
cms FAILED <measurement_id> --json
```

This reads structured `orchestrator_results.jsonl` records from the published
measurement artifacts. It reports the failed feature, config, mode, partition,
config hash, and a rerun command for each failed job.

The normal result destination for non-debug CMS Bench tests is the configured
database path. `debug` tests keep the result destination local to the CMS Client
artifact workflow. The selected mode controls the effective upload behavior.

19. Print stable CLI exit codes for scripts and troubleshooting.

```text
cms EXIT_CODES
```

Use this to distinguish a completed test failure from a connection failure,
protocol mismatch, rejected request, client usage error, or other client-side
operational error.

20. Print troubleshooting guidance for common operator failures.

```text
cms TROUBLESHOOT
```

For one topic:

```text
cms TROUBLESHOOT --topic health
```

The quick start ends here. The remaining sections are reference material for
configuration, command behavior, API details, exit codes, limitations, and
design ownership.

## Operator Recipes

### Run The Default Config

Use this when the bench should execute the feature's persistent
`default.json`.

```text
cms TEST --mode default --feature beacon --config-name default --preview
cms TEST --mode default --feature beacon --config-name default --out-dir artifacts
```

The preview shows the selected config path and hash before submission. The run
uses the most recently staged firmware, watches until a terminal state, then
downloads final artifacts by measurement ID when artifacts are available.

### Run One Exact Named Config

Use this when the bench should execute one named config from the persistent
feature config directory.

```text
cms TEST --mode default --feature beacon --config-name dcdc --preview
cms TEST --mode default --feature beacon --config-name dcdc --out-dir artifacts
```

The client validates and hashes `tests/configs/ble/beacon/dcdc.json`. The daemon
passes that requested config hash to the bench, and the bench validates the same
config before starting pytest. If the config is missing, malformed, outside the
feature directory, or hash-mismatched, the run is rejected before hardware
execution.

### Run A Variable-Filtered Generated Campaign

Use this when the bench should execute every generated config that matches a
declared parameter value, rather than one named config or the whole generated
campaign.

First preview the accepted parameter spelling and job count:

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview
```

For `beacon`, `tests/configs/ble/beacon/parameters.json` declares the parameter
name `phy` with accepted values `coded`, `1m`, and `2m`, so `phy=2m` is valid
for that feature. The same file declares `dcdc` with values `On` and `Off`, so
filters can be intersected:

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --variable dcdc=On --preview
cms TEST --mode full_sweep --feature beacon --variable phy=2m --variable dcdc=On --out-dir artifacts
```

The preview prints `available_variables:` from the selected feature's
`parameters.json`; copy the exact spelling shown there. Do not infer parameter
names from generated filenames. A filter such as `--variable phy=2m` is matched
against the parsed config content through the parameter's declared
`config_path` or `config_paths`.

Variable filters are for generated configs in `one_factor` or `full_sweep`
campaigns. They are intentionally separate from exact config selection:

| Selector | What it chooses |
| --- | --- |
| `--feature beacon` | The feature config directory, such as `tests/configs/ble/beacon` |
| `full_sweep` or `default` mode | Which campaign family to discover |
| `--config-name dcdc` | One exact named config from the feature directory |
| `--variable phy=2m` | Generated configs whose declared `phy` parameter resolves to `2m` |
| `test_orchestrator.py --exact-config ...` | The orchestrator's direct exact-config selector for local/bench internals |

In the CMS Client workflow, `--config-name` and `--variable` are mutually clear:
use `--config-name` for one exact config, or use one or more `--variable`
options for a generated campaign. The bench revalidates names, values, config
hashes, and the final selected job count before hardware execution.

To make local and watched output easier to scan, color can be controlled with:

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --color always --out-dir artifacts
cms WATCH <measurement_id> --verbose --color never
```

`--color auto` is the default. Color is only for human terminal output; JSON,
JUnit XML, retained result artifacts, and `cms FAILED --json` remain plain.

### Rerun A Failed Variable-Filtered Campaign

After a variable-filtered campaign reaches a terminal state, list the failed
jobs:

```text
cms FAILED <measurement_id>
```

For scripts, use:

```text
cms FAILED <measurement_id> --json
```

Each failed record includes the feature, generated config path, mode, partition,
and config hash. For a broad failure pattern, rerun the same filtered campaign:

```text
cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview
cms TEST --mode full_sweep --feature beacon --variable phy=2m --out-dir artifacts
```

For a single failed generated config, use the `rerun:` command printed by
`cms FAILED`, or preview that exact config with `--config-name` after staging
current firmware.

### Attach Or Reconnect

Attach to the currently active measurement:

```text
cms WATCH
```

Attach to a known measurement from the beginning of retained events:

```text
cms WATCH <measurement_id> --cursor 0
```

Resume after the last event cursor already displayed by a previous client:

```text
cms WATCH <measurement_id> --cursor <last_cursor>
```

Show retained stdout, stderr, and lifecycle records while still refreshing the
structured summary:

```text
cms WATCH <measurement_id> --cursor <last_cursor> --verbose
```

Disconnecting the client does not stop the bench measurement. The daemon's
summary and event cursor endpoints are the reconnect source of truth.

### Cancel The Real Run

Cancel the active measurement:

```text
cms CANCEL
```

Then watch until the daemon reports the final cancelled state:

```text
cms WATCH
```

Cancellation is sent to the bench-side execution process. The result is not
complete until the process tree has stopped, output has been drained, and the
daemon reports a terminal state.

### Download Artifacts By Measurement ID

Download all published artifacts for a completed measurement:

```text
cms ARTIFACTS <measurement_id> --out-dir artifacts
```

This always uses the explicit measurement ID. It does not rely on the daemon's
`last` measurement shortcut.

### Identify Failed Tests For Rerun

List failed jobs from the structured campaign result records:

```text
cms FAILED <measurement_id>
```

Emit machine-readable failed-job data for scripts:

```text
cms FAILED <measurement_id> --json
```

The command prefers `orchestrator_results.jsonl` because it contains the CMS job
identity: feature, config, mode, partition, and config hash. Use the printed
`rerun:` command to rerun an individual failed config after staging firmware.

### Troubleshoot Common Failures

Show all troubleshooting topics:

```text
cms TROUBLESHOOT
```

Show one topic:

```text
cms TROUBLESHOOT --topic daemon
```

Troubleshooting topics cover:

- health warnings: run `cms health`, inspect failed or warning checks, and fix
  daemon startup, runtime storage, artifact storage, bench config, tool, board,
  device, and serial mapping issues before submitting tests.
- missing hardware mappings: edit bench-local `cms_bench_runtime.json`; do not
  try to repair hardware mappings through `cms_client_config.json`.
- stale or missing firmware: run `cms BUILD`, then `cms SEND_FW`; `cms TEST`
  uses the most recently staged firmware and does not upload firmware by itself.
- unavailable named configs: run `cms tests`, then
  `cms TEST <mode> <feature> --config-name <name> --preview`; missing,
  malformed, out-of-scope, and hash-mismatched configs are rejected before
  hardware execution.
- daemon connection failures: verify daemon URL/token configuration and use
  `cms EXIT_CODES` to distinguish unreachable daemon errors from protocol
  mismatches.
- truncated live output: continue watching from the oldest retained cursor;
  structured counts and failed identities do not depend on retained console
  text.
- stalled hardware setup: use `cms WATCH <measurement_id> --verbose` and look
  for the last lifecycle setup step without a completion record.
- missing artifacts: wait for a terminal measurement state, then use
  `cms ARTIFACTS <measurement_id> --out-dir artifacts`; use
  `cms FAILED <measurement_id>` for rerun identities.

## What The Client Does

The client performs the parts of a measurement workflow that can happen on a
developer workstation, CI machine, or other automation host:

1. Load client connection settings from `cms_client_config.json`.
2. Query daemon and bench health from `/health`.
3. Query current bench availability from `/bench`.
4. List remotely advertised BLE test features and configs from `/tests`.
5. Build local firmware by invoking `apps/app_manager.py`.
6. Discover built DUT, tester, and optional bootloader images.
7. Resolve and hash the required `sl_bt.xapi`.
8. Build a `MeasurementRequest` containing firmware references, target metadata,
   and request ownership.
9. Send local firmware files to the daemon as base64 payloads with `/firmware`.
10. Start a remote test using the most recently staged firmware with `/test`.
11. Watch the structured summary and cursor-based event endpoints until the run
    reaches a terminal state, preserving the last acknowledged cursor for
    reconnect.
12. Download published artifacts by explicit measurement ID.
13. Request cancellation through `/cancel` or
    `/measurement/<measurement_id>/cancel`.

The current client is focused on one daemon endpoint at a time. It does not yet
perform HWMux discovery, multi-bench scheduling, or database comparison policy.

## Client Configuration

`cms_client_config.json` is the persistent local CMS Client configuration. It
identifies the daemon endpoint, firmware build inputs, target identity, selected
SiSDK, and optional local build-tool paths. These build settings belong here;
they must not be copied into a separate client runtime configuration file.

| Section | Purpose |
| --- | --- |
| `daemon` | Remote daemon connection settings |
| `target` | Target identity and SiSDK selection |
| `build` | Firmware build output and bootloader project settings |
| `tools` | Optional explicit paths to local firmware build tools |

The target section supports:

| Field | Purpose |
| --- | --- |
| `target.board` | Target board identifier used for firmware generation |
| `target.board_rev` | Target board revision recorded with the build and request |
| `target.device` | Target device or part-number identity |
| `target.sisdk` | Selected SiSDK version |
| `target.sisdk_package_path` | Optional explicit path to an unpacked SiSDK package; leave empty when using an installed-SDK resolver |

The build section supports:

| Field | Purpose |
| --- | --- |
| `build.root` | Local root for generated projects, firmware images, runtime XAPI, and build metadata |
| `build.bootloader_slcp` | Bootloader `.slcp` project used by the firmware build |

The tools section supports optional paths for `tools.slt`, `tools.slc`,
`tools.cmake`, and `tools.commander`. A tool value may be left unset when the
corresponding executable can be resolved through the host environment.

The daemon section supports:

- `daemon.base_url`
- `daemon.scheme`
- `daemon.host`
- `daemon.port`
- `daemon.api_token`
- `daemon.timeout_s`

If `base_url` is not provided, the client builds one from
`scheme://host:port`. If `cms_client_config.json` does not exist,
`load_client_config()` falls back to:

```text
base_url = http://<local-hostname>:8000
api_token = silabs-cms
timeout_s = 30.0
```

The current HTTP client adds the token as:

```text
Authorization: Bearer <api_token>
```

The daemon implementation currently exposes routes without enforcing that token,
so authentication is part of the client protocol shape but not yet a complete
security boundary.

## Build Configuration

`cms BUILD` delegates local firmware generation and compilation to:

```text
python apps/app_manager.py <action> <targets...>
```

Supported client build actions are:

- `generate`
- `upgrade`
- `build`
- `rebuild`

`apps/app_manager.py` reads the effective development configuration from
`dev_config.json` and related config-loader behavior. The important firmware
settings are:

- `firmware.board`
- `firmware.device`
- `firmware.sisdk`
- `firmware.build_root`
- `firmware.bootloader_slcp`
- `ble.xapi`
- local tool paths such as `tools.slc`, `tools.cmake`, and `tools.commander`

The app manager discovers installed SiSDK versions, resolves the requested SDK,
trusts it with SLC, generates or upgrades SLCP projects, runs the CMake workflow,
copies `sl_bt.xapi` into the build runtime directory, and writes build metadata.

## Firmware Request Preparation

`cms SEND_FW` prepares a `MeasurementRequest` in one of two ways:

1. Load a complete request from `--request-json`.
2. Build a request from command-line arguments, `cms_client_config.json`, and
   `dev_config.json`.

When no explicit image path is supplied, the client discovers built firmware
under the configured build root. Current discovery looks for:

| Role | Project names searched |
| --- | --- |
| DUT | `ncp_ble_dut_dcdc`, then `ncp_ble_dut` |
| Tester | `ncp_ble_tester` |
| Bootloader | `bootloader-uart-bgapi` |

The request includes:

- DUT image name, path or URL, and optional SHA-256
- Tester image name, path or URL, and optional SHA-256
- Optional bootloader image name, path or URL, and optional SHA-256
- Required `sl_bt.xapi` name, path, and calculated SHA-256
- Board and device metadata
- Git commit, SiSDK version, and SiSDK commit metadata
- `requested_by`
- A generated request ID when one is not supplied

For local files, `send_firmware()` embeds the file bytes in the JSON request as
base64 payloads. For remote URLs, the image URL remains in the request and the
daemon is responsible for fetching it.

## Command Flow

The current CLI commands are:

| Command | Purpose |
| --- | --- |
| `cms health` | Print daemon-plus-bench health checks |
| `cms status` | Print bench state and active measurement ID |
| `cms tests` | List test modes, BLE features, and config names advertised by the daemon |
| `cms EXIT_CODES` | Print stable CLI exit-code meanings |
| `cms TROUBLESHOOT` | Print troubleshooting guidance |
| `cms BUILD` | Build local firmware through `apps/app_manager.py` |
| `cms SEND_FW` | Stage firmware and metadata on the daemon |
| `cms TEST` | Start a remote test, exact config, or variable-filtered campaign using the most recently staged firmware |
| `cms measurement <id>` | Print status for one measurement |
| `cms CANCEL` | Cancel the active or last measurement |
| `cms ARTIFACTS` | Download artifacts from an explicit measurement |
| `cms FAILED` | Show failed test identities and rerun commands |

A typical current workflow is:

1. Build firmware locally.

```text
cms BUILD
```

2. Stage the built images and metadata on the bench daemon.

```text
cms SEND_FW --request-id cms-dev-001 --requested-by <user>
```

3. Run a selected BLE feature using the staged firmware.

```text
cms TEST --mode default --feature beacon --config-name default --out-dir artifacts
```

4. Polling happens automatically during `cms TEST`.
5. When the test passes or fails, the client downloads artifacts by the
   submitted measurement's explicit ID into the configured output directory,
   defaulting to `artifacts`.

## Remote API Surface

`CMSClient` currently calls these daemon routes:

| Method | Route | Client method |
| --- | --- | --- |
| `GET` | `/health` | `get_health()` |
| `GET` | `/bench` | `get_status()` |
| `GET` | `/tests` | `list_tests()` |
| `POST` | `/measurement` | `submit_measurement()` |
| `POST` | `/firmware` | `send_firmware()` |
| `POST` | `/test` | `run_test()` |
| `GET` | `/measurement/<measurement_id>` | `get_measurement()` |
| `POST` | `/measurement/<measurement_id>/cancel` | `cancel_measurement()` |
| `POST` | `/cancel` | `cancel_active_test()` |
| `GET` | `/measurement/<measurement_id>/artifacts` | `download_artifact()` lookup |
| `GET` | `/artifacts/last` | `download_last_artifacts()` |

HTTP errors are converted to `RuntimeError` with the daemon response body.
Connection errors are reported as an unreachable daemon.

## Test Execution Behavior

`cms TEST` does not send firmware by itself. It asks the daemon to run a test
using the most recently staged firmware request. The request body contains:

- measurement mode: `debug`, `default`, `full_sweep`, or `diff`
- feature name
- config name
- zero or more variable filters as `name=value`
- optional request ID
- `upload_results_to_database`

The client sets `upload_results_to_database` to `true` for `default`,
`full_sweep`, and `diff`, and to `false` for `debug`. The daemon applies the
same policy again before bench execution, so older clients that omit or send an
old flag value still follow the bench-side rule.

After the daemon accepts the test, the client polls until the result state is
terminal:

- `passed`
- `failed`
- `cancelled`
- `rejected`
- `interrupted`

The client prints concise counts, the current test, and failed test identities
when structured state changes. `--verbose` also renders timestamped stdout,
stderr, and lifecycle events. When the final result includes artifacts, the CLI
downloads artifacts using that measurement's explicit ID.

Variable filters are repeatable and select the intersection of generated
configs that match every requested parameter value. The accepted parameter names
and values are feature-specific and come from that feature's `parameters.json`.
For `beacon`, the current declared examples include `phy=2m` and `dcdc=On`.
Unknown variables, unsupported values, malformed filters, incompatible exact
config combinations, and zero selected jobs are rejected before pytest or
hardware setup starts.

Human output can be colored with `--color auto|always|never`. The default is
`auto`, which emits ANSI colors only on a TTY. Use `--color always` for local
interactive demos and `--color never` for plain copied logs. Machine-readable
output and artifacts remain uncolored.

The local orchestrator accepts the same variable-filter and color concepts when
running outside the CMS Client workflow:

```text
python test_orchestrator.py tests/configs/ble/beacon --mode full_sweep --feature beacon --variable phy=2m --dry-run --color always
```

This local path writes the same plain `orchestrator_plan.json` and
`orchestrator_results.jsonl` records as before; color affects only the terminal
progress text.

## Artifact Download Behavior

The client can download artifacts by explicit measurement ID:

- `download_artifact(measurement_id, artifact_name, out_dir)` fetches one named
  artifact after looking up the measurement artifact list.
- `download_measurement_artifacts(measurement_id, out_dir)` fetches every
  published artifact for that measurement.

An artifact can be copied or downloaded from:

- a local filesystem path visible to the client
- an absolute HTTP or HTTPS URL
- a daemon-relative URL such as
  `/measurement/<id>/artifacts/<artifact_name>`

Downloaded artifacts preserve the artifact name under the selected output
directory.

## CLI Exit Codes

CMS Client commands use stable process exit codes so scripts and future CI can
distinguish a completed test failure from communication or protocol failures.

| Code | Name | Meaning |
| ---: | --- | --- |
| 0 | `SUCCESS` | Command succeeded or the watched measurement passed |
| 1 | `TEST_FAILED` | The watched measurement completed with failures |
| 2 | `USAGE_ERROR` | Command-line arguments are invalid |
| 3 | `TEST_CANCELLED` | The watched measurement was cancelled |
| 4 | `TEST_REJECTED` | The measurement request was rejected |
| 5 | `CONNECTION_FAILURE` | The daemon is unreachable or a request timed out |
| 6 | `PROTOCOL_MISMATCH` | The daemon protocol/version or response shape is incompatible |
| 7 | `CLIENT_ERROR` | Another daemon, client, configuration, or operational error occurred, including an interrupted run after daemon restart |

Errors are written to stderr. In particular, exit code 1 is reserved for a
real completed test failure; daemon HTTP errors do not masquerade as failed
tests.

## What The Client Does Not Do

The client layer does not:

- Flash DUT or tester hardware directly.
- Control WPKs, serial ports, Joulescope, or GPIOs.
- Own live measurement timing or signal analysis.
- Execute `test_orchestrator.py` on the client host.
- Maintain the daemon runtime workspace.
- Allocate shared benches through HWMux yet.
- Enforce daemon-side authentication.
- Own long-term result storage, baseline selection, or comparison policy.

Those responsibilities belong to the bench, daemon, HWMux integration, database
workflow, or future result-management layer.

## Current Limitations

The current client is useful for bringup and direct daemon interaction, but some
architecture pieces are still in progress:

- The client talks to one configured daemon endpoint at a time.
- HWMux reservation and bench discovery are not implemented in `cms_client.py`.
- `diff` is accepted as a measurement mode but is not a full comparison workflow
  in the client yet.
- `cms_client_config.json` contains a `tools` section, but the current build
  wrapper does not pass that section directly to `apps/app_manager.py`.
- `send_firmware()` sends local files as base64 JSON payloads, which is simple
  for bringup but not the final large-artifact transfer strategy.
- `cms TEST` depends on a previous successful `cms SEND_FW`.
- Database upload is mode-based for CMS Bench tests. Non-debug tests request
  database upload by default; debug tests do not. The client does not upload
  results itself.

## Design Rule

The core architecture rule from `newCMSArch.html` applies directly to this file:

> CMS Bench owns live measurement authority locally. CMS Client owns
> orchestration and workflow.

That rule is why `cms_client.py` focuses on local build orchestration, request
construction, daemon communication, progress polling, and artifact download,
while leaving physical execution and measurement authority on the bench.
