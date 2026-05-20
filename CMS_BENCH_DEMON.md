# CMS Bench Demon Plan

Captured on 2026-05-13.

This document proposes a new bench-side service called `cmsd.py`.

The intent is to run `cmsd.py` on a Raspberry Pi or similar Linux host that is physically attached to one CMS bench. It acts as the network-facing bench manager for remote users, while the existing repo tools continue to own firmware build logic and test execution logic.

## Purpose

`cmsd.py` should:

- listen for remote test requests over the network
- decide whether the bench is available
- grant or deny bench access
- queue test jobs when the bench is busy
- estimate expected wait time and run duration
- retrieve the applications or firmware needed for the run
- launch the local CMS test flow on the bench host
- return job status, logs, and artifact references to the requester

The daemon should be the missing layer between:

- remote users or automation
- the local bench hardware
- existing repo entry points such as `test_orchestrator.py` and `apps/app_manager.py`

## Naming

Working names:

- script: `cmsd.py`
- role: CMS Bench Demon / Daemon
- host role: bench manager

The filename requested here is `CMS_BENCH_DEMON.md`, but the implementation itself should use the conventional term "daemon" in code and docs where practical.

## Core Design Direction

The daemon should support two valid deployment shapes in the same repo:

1. a lightweight Raspberry Pi style bench host that mainly retrieves prebuilt firmware and runs tests
2. a VM or stronger lab host that can both build locally and run tests when the toolchain is available

The safest first architecture is still:

1. keep firmware build and SDK-heavy work off the Raspberry Pi by default
2. let the bench host retrieve prebuilt firmware artifacts over the network
3. let the bench host run flashing, pytest execution, Joulescope capture, and artifact collection locally
4. allow the same `cmsd.py` service to enable local build features when deployed on a VM-class host

This matches the current repo direction:

- `apps/app_manager.py` is local-toolchain oriented and expects `slc`, `slt`, `cmake`, and a usable SiSDK install
- `test_orchestrator.py` already represents a job-style test campaign runner
- the container plan already suggests "build elsewhere, run on bench" as the first realistic Raspberry Pi model
- nothing prevents the same repo from running on a VM that has enough CPU, storage, and toolchain access to build locally

## Bench Responsibilities

`cmsd.py` should own:

- network API
- authentication and authorization
- bench lock / lease state
- job queueing and scheduling
- hardware allocation for DUT, tester, and Joulescope
- test launch and supervision
- artifact publication
- status reporting
- simple run-time estimation

It should not directly absorb all existing logic from other tools.

Instead it should orchestrate:

- `test_orchestrator.py` for multi-config test execution
- `pytest` for single-job execution when needed
- `apps/app_manager.py` only when the bench explicitly supports local builds

## First Version Scope

The first useful version of `cmsd.py` should support:

- one physical bench per daemon instance
- one active job at a time
- one queue for pending jobs
- remote submission of a test request with firmware references
- remote polling for status
- remote cancellation before execution starts
- remote retrieval of result artifacts and logs
- estimated queue wait plus estimated run duration
- lease expiration so a dead client does not hold the bench forever

This is enough to make the bench shareable over the network without solving multi-bench fleet management yet.

## Explicit Non-Goals For V1

- distributed scheduling across many benches
- automatic SiSDK installation from scratch on the Pi
- general multi-tenant artifact storage design
- full database integration
- highly dynamic parallel execution on one bench
- arbitrary user shell access through the daemon

## Proposed Runtime Model

One `cmsd.py` process runs on the bench host and manages:

- bench metadata
- current lease holder
- pending jobs
- active process
- run history cache
- health state of attached hardware

Suggested persistence:

- `artifacts/cmsd/queue.json`
- `artifacts/cmsd/history.jsonl`
- `artifacts/cmsd/state.json`

This can start file-backed before moving to SQLite.

## High-Level Architecture

### 1. Network Layer

Expose a small authenticated HTTP API.

Why HTTP first:

- easy to script from CI and from laptops
- easy to inspect with `curl`
- easy to containerize
- easy to extend later with a thin web UI

Suggested implementation options:

- `FastAPI` if we want structured request/response models quickly
- standard-library `http.server` only if we want the smallest dependency set

For this project, `FastAPI` is likely the most practical option because the daemon is mostly an API and state machine.

### 2. Scheduler Layer

Owns:

- queue ordering
- lease expiration
- admission control
- estimated wait calculations
- job lifecycle transitions

Minimal job states:

- `queued`
- `waiting_for_artifacts`
- `ready`
- `running`
- `passed`
- `failed`
- `cancelled`
- `timed_out`
- `rejected`

### 3. Execution Layer

Owns the actual local subprocess launches.

Examples:

- `uv run python .\test_orchestrator.py ...`
- `uv run pytest ...`
- `uv run python .\apps\app_manager.py build ...` only when enabled

This layer should never build shell strings dynamically when structured argument lists will do.

### 4. Artifact Layer

Owns:

- firmware download staging
- run workspace creation
- log capture
- artifact indexing
- result publication

Suggested directories:

- `artifacts/cmsd/jobs/<job_id>/`
- `artifacts/cmsd/downloads/`
- `artifacts/cmsd/published/`

## Bench Capabilities Model

The daemon should advertise what this bench can do.

Example capabilities:

- `supports_remote_firmware: true`
- `supports_local_build: false`
- `supports_sisdk_fetch: false`
- `supports_joulescope: true`
- `supports_ble_suite: true`
- `supports_bootloader_flash: true`
- `max_queue_depth: 20`

This avoids pretending every bench can build firmware or install SDKs.

## Firmware And Application Strategy

There are three possible input modes. V1 should fully support mode 1 and optionally mode 2.

### Mode 1. Remote Prebuilt Firmware

The request provides:

- DUT image URL
- tester image URL
- optional bootloader image URL
- image metadata such as board, device, commit, and SiSDK version

This should be the default mode on Raspberry Pi benches.

### Mode 2. Repo-Local Build Request

The request provides:

- `.slcp` targets
- requested board
- requested SiSDK identity
- optional repo revision or package reference

The daemon may call `app_manager.py` locally only if:

- the bench advertises `supports_local_build`
- the needed SDK is already installed or can be resolved safely
- the request is authorized for build work

This should be optional because Pi hardware may be too slow or too fragile for full toolchain ownership, while a VM may handle it just fine.

### Mode 3. Hybrid Build Elsewhere, Run Here

The requester or a separate builder service resolves:

- firmware images
- exact SDK provenance
- manifest of produced binaries

Then `cmsd.py` only downloads the manifest payload and executes the run.

This is likely the long-term preferred production shape.

## Firmware Manifest

Instead of sending loose URLs only, define one firmware manifest per job.

Suggested fields:

- `candidate.dut_image_url`
- `candidate.tester_image_url`
- `candidate.bootloader_image_url`
- `candidate.sisdk.version`
- `candidate.sisdk.commit`
- `candidate.app.git_commit`
- `candidate.board`
- `candidate.device`
- `baseline.*`
- `previous.*`

This aligns with the TODO item that SDK identity must be explicit and must not rely only on `SLT`.

## Bench Lease And Permission Model

The daemon should separate bench access from job execution.

### Lease

A client may request a lease such as 30 or 60 minutes.

Lease behavior:

- if bench idle, lease may be granted immediately
- if busy, client is queued
- client must heartbeat periodically
- if heartbeat stops, lease expires
- queued jobs owned by that lease may be cancelled automatically after a grace period

### Permissions

Permission levels could be:

- `viewer`: read status and artifacts
- `runner`: submit and cancel own jobs
- `operator`: drain queue, force cancel, mark bench offline
- `admin`: configure daemon and capability flags

V1 can start with shared token auth and two roles:

- read-only
- operator

## Scheduling Model

For V1, use simple FIFO with optional priority classes.

Suggested job fields:

- `job_id`
- `requested_by`
- `submitted_at`
- `priority`
- `estimated_duration_s`
- `required_capabilities`
- `firmware_manifest`
- `test_selector`
- `config_selector`
- `artifact_retention_days`

Admission rules:

- reject if bench offline
- reject if required hardware capability missing
- reject if required firmware source mode unsupported
- queue if bench busy
- run immediately if idle and artifacts are ready

## Time Estimation

The daemon should estimate:

- queue wait time
- run duration
- total completion ETA

V1 estimation can be simple and still useful.

Inputs:

- requested test feature
- number of configs in the campaign
- whether flashing is needed
- whether firmware downloads are needed
- historical durations from prior jobs

Suggested method:

1. If exact feature plus config mode history exists, use median prior duration.
2. Otherwise estimate from config count times per-config baseline.
3. Add fixed overhead for download, flash, and setup.
4. Queue wait is the sum of remaining estimates ahead in queue.

This can live in a small JSONL-backed history model first.

## Remote API Sketch

Suggested endpoints:

- `GET /health`
- `GET /capabilities`
- `GET /bench`
- `POST /leases`
- `POST /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/logs`
- `GET /jobs/{job_id}/artifacts`
- `POST /jobs/{job_id}/cancel`
- `GET /queue`
- `POST /admin/drain`
- `POST /admin/online`
- `POST /admin/offline`

Example request shape for `POST /jobs`:

```json
{
  "lease_id": "abc123",
  "test_selector": {
    "suite": "ble",
    "feature": "PAwR_obs",
    "mode": "default"
  },
  "firmware_mode": "remote_prebuilt",
  "firmware_manifest": {
    "candidate": {
      "dut_image_url": "https://example/artifacts/dut.s37",
      "tester_image_url": "https://example/artifacts/tester.s37",
      "sisdk": {
        "version": "2025.12.2",
        "commit": "deadbeef"
      },
      "board": "brd2704a"
    }
  }
}
```

## Local Execution Flow

For the default remote-prebuilt path:

1. authenticate request
2. validate lease and capabilities
3. reserve a queue slot
4. download firmware manifest assets into a job workspace
5. validate hashes if provided
6. map firmware into the format expected by the local test flow
7. launch the test runner subprocess
8. stream logs into the job artifact directory
9. collect structured result artifacts
10. publish final status and artifact index

For the optional local-build path:

1. validate the bench allows local builds
2. resolve requested repo/app inputs
3. resolve or verify required SiSDK
4. call `app_manager.py`
5. stage resulting images
6. run the same test flow as above

## Integration With Existing Repo Tools

### `test_orchestrator.py`

Use this as the main execution entry point for campaign-style jobs.

Why:

- it already models a run as jobs over config files
- it already writes structured logs under `artifacts/orchestrator`
- it naturally matches queue-driven bench execution

Potential enhancement later:

- add a machine-readable summary output mode for easier daemon integration

### `apps/app_manager.py`

Use only as a local build helper, not as the daemon itself.

Potential future extension:

- add a non-interactive mode that accepts a manifest or URL inputs
- add a build summary JSON output for daemon consumption

### `tests/conftest.py`

This area will likely need extension so tests can consume firmware images staged by `cmsd.py` instead of assuming only local `.fw_build` discovery.

That aligns with TODO item 2:

- extend firmware provisioning so images can come from URL-backed or daemon-staged locations

## State Machine

Suggested job lifecycle:

1. `queued`
2. `waiting_for_artifacts`
3. `ready`
4. `running`
5. terminal state

Terminal states:

- `passed`
- `failed`
- `cancelled`
- `timed_out`
- `rejected`

Suggested lease lifecycle:

1. `pending`
2. `granted`
3. `expired`
4. `released`
5. `revoked`

## Failure Handling

Common failures to handle explicitly:

- firmware download failed
- artifact checksum mismatch
- requested test feature does not exist
- probe not found
- Joulescope not found
- flashing failed
- test timed out
- bench lost heartbeat or was marked offline

The daemon should always emit:

- failure category
- concise reason
- whether retry is likely useful

## Safety Constraints

The daemon should not:

- expose arbitrary command execution
- accept arbitrary filesystem paths from clients
- overwrite shared firmware directories outside its managed workspace
- allow one user to read another user's private credentials

Recommended V1 safety rules:

- only accept HTTPS artifact URLs from approved domains, or signed URLs
- stage all downloads under one daemon-owned directory
- validate requested test selectors against known suites/features
- run only a fixed allowlist of local commands

## Deployment Shape

Suggested supported deployments:

- one Raspberry Pi per physical bench, mainly for remote firmware retrieval plus test execution
- one VM or stronger Linux host for a bench, with optional local build support enabled
- one `cmsd.py` service per managed bench instance
- one working copy of the repo
- one writable artifacts volume

Optional containerized deployment:

- daemon runs in container
- USB and serial are passed through
- repo workspace is mounted

But V1 should not require full container maturity before the daemon exists.

## Host Profiles

The daemon should expose a host profile so the same code can behave differently without forking the design.

Example profiles:

- `bench-lite`
  - intended for Raspberry Pi style hosts
  - prefers remote-prebuilt firmware
  - local build disabled by default

- `bench-full`
  - intended for VM or workstation style hosts
  - supports remote-prebuilt firmware
  - may also support local `app_manager.py` builds and local SiSDK resolution

This keeps the repo unified while still acknowledging that not every host should carry the same responsibilities.

## Logging And Observability

The daemon should produce:

- service log
- per-job stdout/stderr log
- queue event log
- hardware health snapshots

Useful metadata to record:

- requester
- lease id
- job id
- firmware manifest
- selected suite and feature
- estimated duration
- actual duration
- final exit code

## Suggested File Layout

Possible additions:

- `cmsd.py`
- `cms/daemon_models.py`
- `cms/daemon_queue.py`
- `cms/daemon_runner.py`
- `cms/daemon_artifacts.py`
- `cms/daemon_auth.py`
- `tests/test_cmsd_api.py`
- `tests/test_cmsd_scheduler.py`

If we want the smallest first cut, a single `cmsd.py` is fine, but the queue and runner logic should still be kept in clearly separated classes.

## Recommended Implementation Phases

### Phase 1. Bench Status API

Deliver:

- `GET /health`
- `GET /capabilities`
- static bench metadata
- file-backed state

Success criteria:

- a remote client can discover whether the bench is alive and what it supports

### Phase 2. Lease And Queue

Deliver:

- create lease
- release lease
- submit queued job metadata
- compute ETA

Success criteria:

- multiple users can ask for bench time without colliding

### Phase 3. Remote Firmware Retrieval

Deliver:

- download firmware by manifest
- store under job workspace
- validate basic metadata and optional hashes

Success criteria:

- the bench can run remote-provided images without local builds

### Phase 4. Job Execution

Deliver:

- launch `test_orchestrator.py` or one focused pytest command
- capture logs
- publish final status

Success criteria:

- one remote request can produce one real bench run and visible artifacts

### Phase 5. Historical Timing

Deliver:

- duration history
- better ETA model

Success criteria:

- wait estimates become more realistic after repeated usage

### Phase 6. Optional Local Build Support

Deliver:

- capability-gated call into `app_manager.py`
- requested SiSDK validation
- build summary for staged artifacts

Success criteria:

- a capable VM or bench host can build locally, but the feature remains capability-gated and optional

## Acceptance Criteria For The Overall Feature

We can call the daemon useful when all of the following are true:

- a remote user can see that the bench is online
- a remote user can request access and receive a lease or queue position
- a remote user can submit a job with prebuilt firmware references
- the bench can execute that job without manual intervention
- the bench returns status, logs, and artifact locations
- the daemon prevents conflicting simultaneous use of the same hardware
- the daemon gives a reasonable ETA for waiting and execution

## Open Questions

- Should one daemon own exactly one bench, or should one host manage multiple named benches?
- Do we want token auth only for V1, or is office SSO required?
- Where will firmware artifacts live long term: HTTP server, S3, CI artifacts, or internal package storage?
- Do we want `cmsd.py` to trigger builds itself, or should that responsibility belong to a separate builder service?
- Should the bench publish result summaries back to a database now, or only write artifact files first?

## Recommendation

Implement `cmsd.py` as a bench-local HTTP daemon focused first on:

- bench leasing
- queueing
- ETA calculation
- remote firmware retrieval
- local test execution
- artifact publication

Do not make local SiSDK installation and firmware builds part of the critical path for the first Raspberry Pi deployment, but do keep the daemon design ready to enable that path on a VM-class host in the same repo.

That keeps the first version realistic, matches the existing repo structure, and gives us a clean path to add local build capability later if the bench hardware and SDK distribution story become strong enough.
