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
11. Poll `/measurement/<measurement_id>` until the run reaches a terminal state.
12. Download published artifacts from `/artifacts/last` or a specific
    measurement artifact route.
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
| `cms BUILD` | Build local firmware through `apps/app_manager.py` |
| `cms SEND_FW` | Stage firmware and metadata on the daemon |
| `cms TEST` | Start a remote test using the most recently staged firmware |
| `cms measurement <id>` | Print status for one measurement |
| `cms CANCEL` | Cancel the active or last measurement |
| `cms ARTIFACTS` | Download artifacts from the last completed test |

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
cms TEST default PAwR_obs --config-name default --request-id cms-dev-001-test
```

4. Polling happens automatically during `cms TEST`.
5. When the test passes or fails, the client downloads the last artifacts into
   the configured output directory, defaulting to `artifacts`.

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
- optional request ID
- `upload_results_to_database`

After the daemon accepts the test, the client polls until the result state is
terminal:

- `passed`
- `failed`
- `cancelled`
- `rejected`

The client prints progress whenever the state, percent, or message changes.
When the final result includes artifacts, the CLI downloads the daemon's last
published artifacts.

## Artifact Download Behavior

The client can download artifacts in two modes:

- `download_artifact(measurement_id, artifact_name, out_dir)` fetches one named
  artifact after looking up the measurement artifact list.
- `download_last_artifacts(out_dir)` fetches every artifact from the daemon's
  last completed measurement.

An artifact can be copied or downloaded from:

- a local filesystem path visible to the client
- an absolute HTTP or HTTPS URL
- a daemon-relative URL such as
  `/measurement/<id>/artifacts/<artifact_name>`

Downloaded artifacts preserve the artifact name under the selected output
directory.

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
- Database upload is represented as a request flag and passed to the daemon/test
  flow; the client does not upload results itself.

## Design Rule

The core architecture rule from `newCMSArch.html` applies directly to this file:

> CMS Bench owns live measurement authority locally. CMS Client owns
> orchestration and workflow.

That rule is why `cms_client.py` focuses on local build orchestration, request
construction, daemon communication, progress polling, and artifact download,
while leaving physical execution and measurement authority on the bench.
