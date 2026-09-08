from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cms_client
from cms_protocol import Artifacts, MeasurementResult, MeasurementState


MEASUREMENT_ID = "diff-comparison-001"


def _write_downloaded_diff_artifacts(out_dir: Path, *, child_current: float) -> list[Path]:
    plan = out_dir / "results" / "paired_default_plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "feature": "beacon",
                        "config_path": "tests/configs/ble/beacon/default.json",
                        "config_hash": "config-sha256",
                        "partition": "",
                        "mode": "default",
                        "firmware_role": "dcdc",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    paths = [plan]
    for variant, current in (("parent", 1.0), ("child", child_current)):
        path = out_dir / "results" / variant / "beacon.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "test_run": {
                        "test_name": "beacon",
                        "config_path": "tests/configs/ble/beacon/default.json",
                        "config_hash": "config-sha256",
                    },
                    "dataset": {"mode": "default", "partition": ""},
                    "firmware": {"board": "brd4182a", "device": "EFR32MG22"},
                    "result_metric": {
                        "avg_current_ma": current,
                        "peak_current_ma": current,
                        "rx_current_ma": current,
                        "tx_current_ma": current,
                    },
                    "result_summary": [{"meas_type": "Interval", "i_mean_ma": current}],
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def _install_diff_client(monkeypatch, out_dir: Path, *, child_current: float):
    class Client:
        base_url = "http://bench.test"

        def run_test(self, *args, **kwargs):
            return MeasurementResult(measurement_id=MEASUREMENT_ID, state=MeasurementState.ACCEPTED)

        def get_measurement(self, measurement_id):
            return MeasurementResult(
                measurement_id=measurement_id,
                state=MeasurementState.PASSED,
                artifacts=[Artifacts(name="results/paired_default_plan.json")],
            )

        def download_measurement_artifacts(self, measurement_id, requested_out_dir):
            assert measurement_id == MEASUREMENT_ID
            assert requested_out_dir == out_dir
            return _write_downloaded_diff_artifacts(out_dir, child_current=child_current)

    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: Client())
    monkeypatch.setattr(
        cms_client,
        "_watch_remote_measurement",
        lambda *args, **kwargs: SimpleNamespace(
            summary=SimpleNamespace(state=MeasurementState.PASSED)
        ),
    )


def test_diff_client_saves_report_and_renders_table_after_successful_execution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_diff_client(monkeypatch, tmp_path, child_current=1.016)

    exit_code = cms_client.main(
        ["test", "--mode", "diff", "--feature", "all", "--out-dir", str(tmp_path)]
    )

    output = capsys.readouterr().out
    assert exit_code == int(cms_client.CMSCLIExitCode.SUCCESS)
    assert (tmp_path / "diff_comparison.json").is_file()
    assert "| Variable | Child | Parent | Delta (child-parent) | Percent | Status |" in output


def test_diff_client_returns_regression_exit_code_after_successful_execution(
    tmp_path: Path, monkeypatch
) -> None:
    _install_diff_client(monkeypatch, tmp_path, child_current=1.02)

    exit_code = cms_client.main(
        ["test", "--mode", "diff", "--feature", "all", "--out-dir", str(tmp_path)]
    )

    assert exit_code == int(cms_client.CMSCLIExitCode.THRESHOLD_REGRESSION)


def test_diff_client_renders_the_saved_report_not_the_in_memory_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_diff_client(monkeypatch, tmp_path, child_current=1.016)

    def write_replaced_report(report, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps({"pairs": [], "diagnostics": []}), encoding="utf-8")
        return output_path

    monkeypatch.setattr(cms_client, "write_diff_comparison_report", write_replaced_report)

    exit_code = cms_client.main(
        ["test", "--mode", "diff", "--feature", "all", "--out-dir", str(tmp_path)]
    )

    output = capsys.readouterr().out
    assert exit_code == int(cms_client.CMSCLIExitCode.SUCCESS)
    assert "No comparable metrics." in output
    assert "avg_current_ma" not in output


def test_diff_execution_failure_takes_precedence_over_comparison_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    class Client:
        base_url = "http://bench.test"

        def run_test(self, *args, **kwargs):
            return MeasurementResult(measurement_id=MEASUREMENT_ID, state=MeasurementState.ACCEPTED)

        def get_measurement(self, measurement_id):
            return MeasurementResult(measurement_id=measurement_id, state=MeasurementState.FAILED)

    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: Client())
    monkeypatch.setattr(
        cms_client,
        "_watch_remote_measurement",
        lambda *args, **kwargs: SimpleNamespace(
            summary=SimpleNamespace(state=MeasurementState.FAILED)
        ),
    )

    exit_code = cms_client.main(
        ["test", "--mode", "diff", "--feature", "all", "--out-dir", str(tmp_path)]
    )

    assert exit_code == int(cms_client.CMSCLIExitCode.TEST_FAILED)
    assert not (tmp_path / "diff_comparison.json").exists()
