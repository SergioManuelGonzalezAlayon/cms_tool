import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cms_colors import ColorFormatter
import test_orchestrator as orchestrator


def _job(name: str) -> orchestrator.Job:
    return orchestrator.Job(
        suite="ble",
        feature="beacon",
        nodeid="tests/ble.py::test_beacon",
        config_path=Path(f"tests/configs/ble/beacon/{name}.json"),
        cfg_arg=f"beacon/{name}.json",
        mode="default",
        partition="default",
        dut_slcp="dut.slcp",
        tester_slcp="tester.slcp",
        board="brd4187c",
        supply_v=3.0,
        config_hash=f"hash-{name}",
        measurement_id="nightly-20260818T093000000000Z-abc123",
        run_name="Nightly Beacon Run",
        measurement_created_at="2026-08-18T09:30:00+00:00",
    )


def test_job_plan_preserves_complete_discovery_order():
    jobs = [_job("first"), _job("second"), _job("third")]

    plan = orchestrator.build_job_plan(jobs)

    assert plan["record_type"] == "job_plan"
    assert plan["schema_version"] == 1
    assert plan["total_jobs"] == 3
    assert [record["job_id"] for record in plan["jobs"]] == [job.job_id for job in jobs]
    assert [record["ordinal"] for record in plan["jobs"]] == [1, 2, 3]
    assert all(record["total_jobs"] == 3 for record in plan["jobs"])
    assert all(record["measurement_id"] == jobs[0].measurement_id for record in plan["jobs"])
    assert all(record["run_name"] == "Nightly Beacon Run" for record in plan["jobs"])
    assert all(record["status"] == "pending" for record in plan["jobs"])
    assert [record["config_hash"] for record in plan["jobs"]] == [
        "hash-first",
        "hash-second",
        "hash-third",
    ]


def test_main_writes_complete_plan_before_first_pytest_job(tmp_path, monkeypatch):
    jobs = [_job("first"), _job("second")]
    results_log = tmp_path / "orchestrator_results.jsonl"
    observed_job_ids = []
    monkeypatch.setattr(orchestrator, "build_jobs", lambda *args, **kwargs: jobs)

    def fake_run_job(job, pytest_args, junit_path):
        plan_path = tmp_path / "orchestrator_plan.json"
        assert plan_path.exists()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert [record["job_id"] for record in plan["jobs"]] == [item.job_id for item in jobs]
        assert plan["jobs"][jobs.index(job)]["junit_path"] == str(junit_path)
        records = [
            json.loads(line)
            for line in results_log.read_text(encoding="utf-8").splitlines()
        ]
        assert records[-1]["record_type"] == "job_started"
        assert records[-1]["job_id"] == job.job_id
        assert records[-1]["status"] == "running"
        observed_job_ids.append(job.job_id)
        return 0, 0.01

    monkeypatch.setattr(orchestrator, "run_job", fake_run_job)

    exit_code = orchestrator.main(["--results-log", str(results_log)])

    assert exit_code == 0
    assert observed_job_ids == [job.job_id for job in jobs]
    records = [json.loads(line) for line in results_log.read_text(encoding="utf-8").splitlines()]
    assert [record["record_type"] for record in records] == [
        "job_started",
        "job_completed",
        "job_started",
        "job_completed",
    ]
    assert [record["status"] for record in records] == [
        "running",
        "passed",
        "running",
        "passed",
    ]
    assert [record["ordinal"] for record in records] == [1, 1, 2, 2]
    for record in records:
        assert record["measurement_id"] == jobs[0].measurement_id
        assert record["run_name"] == jobs[0].run_name
        assert record["feature"] == "beacon"
        assert record["config_hash"]
        assert record["mode"] == "default"
        assert record["partition"] == "default"
    for started, completed in zip(records[::2], records[1::2]):
        assert completed["started_at"] == started["started_at"]
        assert completed["result_type"] == "pytest_junit"
        assert datetime.fromisoformat(started["started_at"]).utcoffset().total_seconds() == 0
        assert datetime.fromisoformat(completed["finished_at"]).utcoffset().total_seconds() == 0


def test_resume_plan_keeps_jobs_that_execution_skips(tmp_path, monkeypatch):
    jobs = [_job("already-passed"), _job("remaining")]
    results_log = tmp_path / "orchestrator_results.jsonl"
    orchestrator.append_result(
        results_log,
        {"job_id": jobs[0].job_id, "status": "passed"},
    )
    original_log = results_log.read_bytes()
    executed = []
    monkeypatch.setattr(orchestrator, "build_jobs", lambda *args, **kwargs: jobs)
    monkeypatch.setattr(
        orchestrator,
        "run_job",
        lambda job, pytest_args, junit_path: (executed.append(job.job_id) or 0, 0.01),
    )

    exit_code = orchestrator.main(["--resume", "--results-log", str(results_log)])
    plan = json.loads((tmp_path / "orchestrator_plan.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert results_log.read_bytes().startswith(original_log)
    assert [record["job_id"] for record in plan["jobs"]] == [job.job_id for job in jobs]
    assert [record["ordinal"] for record in plan["jobs"]] == [1, 2]
    assert executed == [jobs[1].job_id]
    records = [json.loads(line) for line in results_log.read_text(encoding="utf-8").splitlines()]
    assert records[-2]["record_type"] == "job_started"
    assert records[-2]["ordinal"] == 2
    assert records[-2]["total_jobs"] == 2
    assert records[-1]["record_type"] == "job_completed"
    assert records[-1]["ordinal"] == 2


def test_completed_job_reader_separates_terminal_records_and_supports_legacy(tmp_path):
    results_log = tmp_path / "orchestrator_results.jsonl"
    records = [
        {"job_id": "legacy-pass", "status": "passed"},
        {"record_type": "job_started", "job_id": "active", "status": "running"},
        {"record_type": "job_completed", "job_id": "new-pass", "status": "passed"},
        {"record_type": "job_completed", "job_id": "new-fail", "status": "failed"},
    ]
    for record in records:
        orchestrator.append_result(results_log, record)

    completed = orchestrator.completed_job_records(results_log)

    assert [record["job_id"] for record in completed] == [
        "legacy-pass",
        "new-pass",
        "new-fail",
    ]
    assert orchestrator.completed_job_ids(results_log) == {"legacy-pass", "new-pass"}


def test_failed_job_gets_terminal_failed_record(tmp_path, monkeypatch):
    job = _job("failure")
    results_log = tmp_path / "orchestrator_results.jsonl"
    monkeypatch.setattr(orchestrator, "build_jobs", lambda *args, **kwargs: [job])
    monkeypatch.setattr(orchestrator, "run_job", lambda job, pytest_args, junit_path: (7, 1.2345))

    exit_code = orchestrator.main(["--results-log", str(results_log)])
    records = [json.loads(line) for line in results_log.read_text(encoding="utf-8").splitlines()]

    assert exit_code == 1
    assert records[-1]["record_type"] == "job_completed"
    assert records[-1]["status"] == "failed"
    assert records[-1]["exit_code"] == 7
    assert records[-1]["duration_s"] == 1.234
    assert records[-1]["result_type"] == "pytest_junit"


def test_print_plan_can_force_or_disable_color(capsys):
    jobs = [_job("default")]

    orchestrator.print_plan(jobs, colors=ColorFormatter(True))
    forced_output = capsys.readouterr().out
    orchestrator.print_plan(jobs, colors=ColorFormatter(False))
    plain_output = capsys.readouterr().out

    assert "\033[" in forced_output
    assert "\033[" not in plain_output


def test_orchestrator_color_option_only_affects_human_output(tmp_path, monkeypatch, capsys):
    job = _job("default")
    results_log = tmp_path / "orchestrator_results.jsonl"
    monkeypatch.setattr(orchestrator, "build_jobs", lambda *args, **kwargs: [job])

    exit_code = orchestrator.main(
        [
            "--color",
            "always",
            "--dry-run",
            "--results-log",
            str(results_log),
        ]
    )

    output = capsys.readouterr().out
    plan = json.loads((tmp_path / "orchestrator_plan.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "\033[" in output
    assert "\033[" not in json.dumps(plan)


def test_build_jobs_hashes_config_and_propagates_measurement_identity(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    config_path = feature_dir / "default.json"
    config_path.write_text('{"hw":{"supply_v":3.0}}\n', encoding="utf-8")
    metadata_path = tmp_path / "cms_fw_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "identity": {
                    "measurement_id": "measurement-123",
                    "run_name": "Readable Run",
                    "created_at": "2026-08-18T09:30:00+00:00",
                },
                "firmware": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    jobs = orchestrator.build_jobs(
        [config_root],
        mode="default",
        feature_filters=set(),
        partition_filters=set(),
        cms_metadata_path=str(metadata_path),
    )

    assert len(jobs) == 1
    assert jobs[0].config_hash == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert jobs[0].measurement_id == "measurement-123"
    assert jobs[0].run_name == "Readable Run"
    assert jobs[0].measurement_created_at == "2026-08-18T09:30:00+00:00"


def test_build_jobs_propagates_requested_config_hash_from_cms_metadata(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    config_path = feature_dir / "default.json"
    config_path.write_text("{}\n", encoding="utf-8")
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    metadata_path = tmp_path / "cms_fw_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "request": {
                    "selector": {
                        "mode": "ble",
                        "feature": "beacon",
                        "config": "default",
                        "config_hash": config_hash,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    jobs = orchestrator.build_jobs(
        [config_root],
        mode="default",
        feature_filters=set(),
        partition_filters=set(),
        cms_metadata_path=str(metadata_path),
    )
    plan = orchestrator.build_job_plan(jobs)

    assert jobs[0].requested_config_hash == config_hash
    assert jobs[0].config_hash == config_hash
    assert plan["jobs"][0]["requested_config_hash"] == config_hash
    assert plan["jobs"][0]["config_hash"] == jobs[0].config_hash


def test_build_jobs_rejects_requested_config_hash_mismatch(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    (feature_dir / "default.json").write_text("{}\n", encoding="utf-8")
    metadata_path = tmp_path / "cms_fw_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "request": {
                    "selector": {
                        "mode": "ble",
                        "feature": "beacon",
                        "config": "default",
                        "config_hash": "not-the-default-hash",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    with pytest.raises(ValueError, match="requested config hash does not match"):
        orchestrator.build_jobs(
            [config_root],
            mode="default",
            feature_filters=set(),
            partition_filters=set(),
            cms_metadata_path=str(metadata_path),
        )


def test_orchestrator_validates_variable_filters_from_parameters_json(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps({"parameters": [{"name": "phy", "values": ["1m", "2m"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    filters = orchestrator.parse_variable_filters(["phy=2m"])

    orchestrator.validate_variable_filters_for_targets(
        [config_root],
        feature_filters={"beacon"},
        variable_filters=filters,
    )


def test_orchestrator_rejects_unknown_variable_before_building_jobs(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps({"parameters": [{"name": "phy", "values": ["1m", "2m"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    exit_code = orchestrator.main(
        [
            str(config_root),
            "--feature",
            "beacon",
            "--variable",
            "unknown=2m",
        ]
    )

    assert exit_code == 2


def test_build_jobs_filters_generated_configs_by_declared_variable_value(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    sweep_dir = feature_dir / "full_sweep" / "dcdc"
    sweep_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "phy",
                        "values": ["1m", "2m", "coded"],
                        "config_paths": ["advertiser.phy", "scanner.phy"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (feature_dir / "default.json").write_text('{"advertiser":{"phy":"1m"}}\n', encoding="utf-8")
    one_factor_2m = feature_dir / "phy_2m.json"
    one_factor_2m.write_text(
        '{"advertiser":{"phy":"2m"},"scanner":{"phy":"1m"}}\n',
        encoding="utf-8",
    )
    one_factor_coded = feature_dir / "phy_coded.json"
    one_factor_coded.write_text(
        '{"advertiser":{"phy":"coded"},"scanner":{"phy":"coded"}}\n',
        encoding="utf-8",
    )
    full_sweep_2m = sweep_dir / "case_2m.json"
    full_sweep_2m.write_text(
        '{"advertiser":{"phy":"2m"},"scanner":{"phy":"1m"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    filters = orchestrator.parse_variable_filters(["phy=2m"])
    jobs = orchestrator.build_jobs(
        [config_root],
        mode="all",
        feature_filters={"beacon"},
        partition_filters=set(),
        variable_filters=filters,
    )
    plan = orchestrator.build_job_plan(jobs)

    assert [job.cfg_arg for job in jobs] == [
        "phy_2m.json",
        "full_sweep/dcdc/case_2m.json",
    ]
    assert all(job.variable_filters == filters for job in jobs)
    assert plan["variable_filters"] == [{"name": "phy", "value": "2m"}]
    assert all(
        record["variable_filters"] == [{"name": "phy", "value": "2m"}]
        for record in plan["jobs"]
    )


def test_build_jobs_variable_filters_use_declared_option_mappings(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "dcdc",
                        "values": ["On", "Off"],
                        "config_path": "firmware.dut_slcp",
                        "options": {
                            "On": "apps/ncp_ble_dut_dcdc.slcp",
                            "Off": "apps/ncp_ble_dut.slcp",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (feature_dir / "dcdc.json").write_text(
        '{"firmware":{"dut_slcp":"apps/ncp_ble_dut_dcdc.slcp"}}\n',
        encoding="utf-8",
    )
    (feature_dir / "no_dcdc.json").write_text(
        '{"firmware":{"dut_slcp":"apps/ncp_ble_dut.slcp"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    jobs = orchestrator.build_jobs(
        [config_root],
        mode="one_factor",
        feature_filters={"beacon"},
        partition_filters=set(),
        variable_filters=orchestrator.parse_variable_filters(["dcdc=On"]),
    )

    assert [job.cfg_arg for job in jobs] == ["dcdc.json"]


def test_build_jobs_variable_filters_select_intersection(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    sweep_dir = feature_dir / "full_sweep" / "dcdc"
    sweep_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "phy",
                        "values": ["1m", "2m"],
                        "config_path": "advertiser.phy",
                    },
                    {
                        "name": "tx_power",
                        "values": ["0 dBm", "10 dBm"],
                        "config_path": "advertiser.tx_power_label",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (sweep_dir / "2m_0dbm.json").write_text(
        '{"advertiser":{"phy":"2m","tx_power_label":"0 dBm"}}\n',
        encoding="utf-8",
    )
    (sweep_dir / "2m_10dbm.json").write_text(
        '{"advertiser":{"phy":"2m","tx_power_label":"10 dBm"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    jobs = orchestrator.build_jobs(
        [config_root],
        mode="full_sweep",
        feature_filters={"beacon"},
        partition_filters=set(),
        variable_filters=orchestrator.parse_variable_filters(["phy=2m", "tx_power=10 dBm"]),
    )

    assert [job.cfg_arg for job in jobs] == ["full_sweep/dcdc/2m_10dbm.json"]


def test_orchestrator_rejects_zero_match_variable_filters_before_pytest(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "phy",
                        "values": ["1m", "2m"],
                        "config_path": "advertiser.phy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (feature_dir / "phy_1m.json").write_text(
        '{"advertiser":{"phy":"1m"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(
        orchestrator,
        "run_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pytest must not start for zero-match filters")
        ),
    )

    exit_code = orchestrator.main(
        [
            str(config_root),
            "--mode",
            "one_factor",
            "--feature",
            "beacon",
            "--variable",
            "phy=2m",
        ]
    )

    assert exit_code == 1


def test_orchestrator_dry_run_writes_variable_filtered_plan(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "phy",
                        "values": ["1m", "2m"],
                        "config_path": "advertiser.phy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (feature_dir / "phy_2m.json").write_text(
        '{"advertiser":{"phy":"2m"}}\n',
        encoding="utf-8",
    )
    results_log = tmp_path / "orchestrator_results.jsonl"
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    exit_code = orchestrator.main(
        [
            str(config_root),
            "--mode",
            "one_factor",
            "--feature",
            "beacon",
            "--variable",
            "phy=2m",
            "--dry-run",
            "--results-log",
            str(results_log),
        ]
    )
    plan = json.loads((tmp_path / "orchestrator_plan.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert plan["total_jobs"] == 1
    assert plan["variable_filters"] == [{"name": "phy", "value": "2m"}]
    assert plan["jobs"][0]["cfg_arg"] == "phy_2m.json"


def test_build_jobs_can_select_one_exact_named_config(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    (feature_dir / "default.json").write_text('{"hw":{"supply_v":3.0}}\n', encoding="utf-8")
    exact_path = feature_dir / "dcdc.json"
    exact_path.write_text('{"hw":{"supply_v":2.4}}\n', encoding="utf-8")
    (feature_dir / "payload_63bytes.json").write_text('{"hw":{"supply_v":1.8}}\n', encoding="utf-8")
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    jobs = orchestrator.build_jobs(
        [config_root],
        mode="all",
        feature_filters=set(),
        partition_filters=set(),
        exact_config="dcdc",
    )

    assert len(jobs) == 1
    assert jobs[0].cfg_arg == "dcdc.json"
    assert jobs[0].config_path == exact_path
    assert jobs[0].config_hash == hashlib.sha256(exact_path.read_bytes()).hexdigest()


def test_exact_named_config_rejects_missing_and_out_of_scope_paths(tmp_path, monkeypatch):
    config_root = tmp_path / "configs"
    feature_dir = config_root / "ble" / "beacon"
    feature_dir.mkdir(parents=True)
    (feature_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    (feature_dir / "default.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "CONFIG_ROOT", config_root)

    with pytest.raises(ValueError, match="exact config was not found"):
        orchestrator.build_jobs(
            [config_root],
            mode="all",
            feature_filters=set(),
            partition_filters=set(),
            exact_config="missing",
        )

    with pytest.raises(ValueError, match="single relative config path"):
        orchestrator.build_jobs(
            [config_root],
            mode="all",
            feature_filters=set(),
            partition_filters=set(),
            exact_config="../default",
        )


def test_junit_paths_are_unique_inside_each_measurement_workspace(tmp_path):
    first_workspace = tmp_path / "measurement-one"
    second_workspace = tmp_path / "measurement-two"
    job = _job("Default Config")

    first = orchestrator.junit_path_for_job(first_workspace / "orchestrator_results.jsonl", job, 1)
    next_job = orchestrator.junit_path_for_job(first_workspace / "orchestrator_results.jsonl", job, 2)
    second = orchestrator.junit_path_for_job(second_workspace / "orchestrator_results.jsonl", job, 1)

    assert first == first_workspace / "junit" / "0001-beacon-default-config.xml"
    assert len({first, next_job, second}) == 3


def test_run_job_passes_authoritative_unique_junit_path(tmp_path, monkeypatch):
    job = _job("default")
    junit_path = tmp_path / "junit" / "0001-beacon-default.xml"
    captured = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_subprocess_run)

    returncode, _ = orchestrator.run_job(
        job,
        ["--junitxml", "unwanted-global.xml"],
        junit_path,
    )

    assert returncode == 0
    assert junit_path.parent.is_dir()
    assert ["-o", "addopts="] == captured["cmd"][3:5]
    assert captured["cmd"][-2:] == ["--junitxml", str(junit_path)]


def test_orchestrated_job_does_not_write_configured_global_junit(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--junitxml=artifacts/pytest-results.xml"\n',
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--cfg')\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    job = replace(
        _job("default"),
        nodeid="test_sample.py::test_ok",
        config_path=tmp_path / "default.json",
    )
    junit_path = tmp_path / "measurement" / "junit" / "0001-beacon-default.xml"
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)

    returncode, _ = orchestrator.run_job(job, [], junit_path)

    assert returncode == 0
    assert junit_path.exists()
    assert not (tmp_path / "artifacts" / "pytest-results.xml").exists()


def test_standalone_orchestrator_still_strips_forwarded_pytest_cfg(tmp_path, monkeypatch):
    job = _job("default")
    results_log = tmp_path / "orchestrator_results.jsonl"
    observed_pytest_args = []
    monkeypatch.setattr(orchestrator, "build_jobs", lambda *args, **kwargs: [job])
    monkeypatch.setattr(
        orchestrator,
        "run_job",
        lambda job, pytest_args, junit_path: (
            observed_pytest_args.extend(pytest_args) or 0,
            0.01,
        ),
    )

    exit_code = orchestrator.main(
        [
            "--results-log",
            str(results_log),
            "--",
            "--cfg",
            "dcdc.json",
        ]
    )

    assert exit_code == 0
    assert "--cfg" not in observed_pytest_args
    assert "dcdc.json" not in observed_pytest_args


def test_cms_orchestrator_rejects_forwarded_pytest_cfg(tmp_path, monkeypatch, capsys):
    metadata_path = tmp_path / "cms_fw_metadata.json"
    metadata_path.write_text('{"request":{"selector":{}}}\n', encoding="utf-8")

    exit_code = orchestrator.main(
        [
            "--cms-fw-metadata",
            str(metadata_path),
            "--cfg",
            "dcdc.json",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "CMS orchestrator runs must use --exact-config" in output
