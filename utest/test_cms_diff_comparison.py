import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from cms_diff_comparison import (
    DifferentialPairingError,
    PercentageThresholdPolicy,
    ResultMetricParseError,
    SampleStatistics,
    create_diff_comparison_report,
    extract_measurement_metrics,
    pair_downloaded_differential_results,
    prepare_comparison_inputs,
    render_diff_comparison_table,
    summarize_sample_values,
    write_diff_comparison_report,
    write_diff_excel_report,
)


def test_workbook_links_each_result_side_to_its_sdk_metadata(tmp_path: Path) -> None:
    record = _normalized_report_record("beacon", "PASS")
    record["version1"]["test_configuration"] = {"advertiser": {"interval": 200, "tx_power": 0}}
    record["version1"]["artifact_path"] = "version1/result.json"
    for side, version, build, revision in (("version1", "2026.6.1", "101", "aaaa"),
                                           ("version2", "2026.6.2", "202", "bbbb")):
        record[side]["provenance"]["sisdk_metadata"] = {
            "status": "complete", "version": version, "build_number": build,
            "libraries": [f"bluetooth/1.0@silabs/release#{revision}%123"],
            "conan_lock": {"source": "artifactory", "sha256": revision},
        }
    output = write_diff_excel_report({"records": [record]}, tmp_path / "report.xlsx")
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == ["Differential Results", "Comparison rules", "SiSDK_2026.6.1_101", "SiSDK_2026.6.2_202"]
        assert workbook["SiSDK_2026.6.1_101"]["C6"].value == "aaaa"
        assert workbook["SiSDK_2026.6.2_202"]["C6"].value == "bbbb"
        results = workbook["Differential Results"]
        assert "SiSDK_2026.6.1_101" in results["K2"].hyperlink.target
        assert "SiSDK_2026.6.2_202" in results["L2"].hyperlink.target
        assert '"interval": 200' in results["N2"].value
        assert "V2: configuration unavailable" in results["N2"].value
        assert "version1/result.json" in results["O2"].value
        assert "V2 − V1" in workbook["Comparison rules"]["B1"].value
    finally:
        workbook.close()


def _write_plan(path: Path, *, role: str = "dcdc", partition: str = "") -> None:
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "feature": "beacon",
                        "config_path": "tests/configs/ble/beacon/default.json",
                        "config_hash": "config-sha256",
                        "partition": partition,
                        "mode": "default",
                        "firmware_role": role,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_result(root: Path, filename: str, *, config_hash: str = "config-sha256") -> Path:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "test_run": {
                    "test_name": "beacon",
                    "config_path": "tests\\configs\\ble\\beacon\\default.json",
                    "config_hash": config_hash,
                },
                "dataset": {"mode": "default", "partition": ""},
                "firmware": {"board": "brd4182a", "device": "EFR32MG22"},
                "result_metric": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sampled_plan(
    path: Path,
    *,
    samples: int = 2,
    roles: tuple[str, ...] = ("dcdc", "no_dcdc"),
) -> None:
    jobs = []
    dut_projects = {
        "dcdc": "ncp_ble_dut_dcdc",
        "no_dcdc": "ncp_ble_dut",
    }
    for role in roles:
        for sample_index in range(1, samples + 1):
            jobs.append(
                {
                    "feature": "beacon",
                    "config_path": "tests/configs/ble/beacon/default.json",
                    "config_hash": "config-sha256",
                    "partition": "",
                    "mode": "default",
                    "board": "brd4182a",
                    "supply_v": 3.3,
                    "dut_slcp": f"apps/{dut_projects[role]}/{dut_projects[role]}.slcp",
                    "firmware_role": role,
                    "sample_index": sample_index,
                }
            )
    path.write_text(
        json.dumps(
            {
                "samples": samples,
                "selector": {"config": "default"},
                "variable_filters": [],
                "jobs": jobs,
            }
        ),
        encoding="utf-8",
    )


def _write_sampled_result(
    root: Path,
    *,
    variant: str,
    role: str,
    sample_index: int,
    sdk_version: str | None = None,
    filename: str | None = None,
) -> Path:
    dut_project = "ncp_ble_dut_dcdc" if role == "dcdc" else "ncp_ble_dut"
    sdk_version = sdk_version or ("2025.12.0" if variant == "version1" else "2026.6.0")
    path = root / (filename or f"{role}_sample_{sample_index:03d}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    scale = 1.0 if variant == "version1" else 1.1
    interval_current = float(sample_index * 2 - 1) * scale
    avg_current = float(sample_index + 1) * scale
    peak_current = float(sample_index * 2 + 8) * scale
    event_energy = float(10 + (sample_index - 1) * 4) * scale
    interval_energy = float(20 + (sample_index - 1) * 4) * scale
    rx_current = None if role == "dcdc" else float(sample_index + 2) * scale
    tx_current = None if role == "dcdc" else float(sample_index + 3) * scale
    path.write_text(
        json.dumps(
            {
                "test_run": {
                    "test_name": "beacon",
                    "config_path": "tests\\configs\\ble\\beacon\\default.json",
                    "config_hash": "config-sha256",
                    "sample_index": sample_index,
                },
                "dataset": {
                    "mode": "default",
                    "partition": "",
                    "sdk_version": sdk_version,
                },
                "firmware": {
                    "board": "brd4182a",
                    "device": "EFR32MG22",
                    "dut_slcp": f"apps/{dut_project}/{dut_project}.slcp",
                    "firmware_role": role,
                    "variant": variant,
                    "dut_sdk_version": sdk_version,
                },
                "parameters": {"supply_voltage_v": 3.3},
                "result_metric": {
                    "avg_current_ma": avg_current,
                    "peak_current_ma": peak_current,
                    "rx_current_ma": rx_current,
                    "tx_current_ma": tx_current,
                    "metric_status": {
                        "avg_current_ma": "measured",
                        "peak_current_ma": "measured",
                        "rx_current_ma": "unavailable" if rx_current is None else "measured",
                        "tx_current_ma": "unavailable" if tx_current is None else "measured",
                    },
                },
                "result_summary": [
                    {"meas_type": "Event", "e_mean_nj": event_energy},
                    {
                        "meas_type": "Interval",
                        "i_mean_ma": interval_current,
                        "e_mean_nj": interval_energy,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sampled_campaign(
    tmp_path: Path,
    *,
    samples: int = 2,
    roles: tuple[str, ...] = ("dcdc", "no_dcdc"),
) -> tuple[Path, Path, Path]:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_sampled_plan(plan_path, samples=samples, roles=roles)
    for variant, root in (("version1", version1_root), ("version2", version2_root)):
        for role in roles:
            for sample_index in range(1, samples + 1):
                _write_sampled_result(
                    root,
                    variant=variant,
                    role=role,
                    sample_index=sample_index,
                )
    return version1_root, version2_root, plan_path


def test_pairs_version1_and_version2_by_stable_identity_not_filename_or_order(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(version1_root, "arbitrary-version1-name.json")
    _write_result(version2_root, "different-version2-name.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.firmware_role == "dcdc"
    assert pairing.pairs[0].version1.variant == "version1"
    assert pairing.pairs[0].version2.variant == "version2"
    assert not pairing.unmatched_version1
    assert not pairing.unmatched_version2


def test_pairs_absolute_plan_config_paths_with_repo_relative_result_paths(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["jobs"][0]["config_path"] = "/home/admin/current_measurement_tool/tests/configs/ble/beacon/default.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _write_result(version1_root, "version1.json")
    _write_result(version2_root, "version2.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.config_path == "tests/configs/ble/beacon/default.json"


def test_resolves_missing_result_partition_from_the_paired_plan(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path, partition="dcdc")
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path in (version1_path, version2_path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["dataset"]["partition"]
        path.write_text(json.dumps(payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.partition == "dcdc"


def test_retains_a_missing_version2_as_an_unmatched_version1_result(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path, role="no_dcdc")
    _write_result(version1_root, "version1-only.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert not pairing.pairs
    assert len(pairing.unmatched_version1) == 1
    assert pairing.unmatched_version1[0].identity.firmware_role == "no_dcdc"
    assert not pairing.unmatched_version2


def test_rejects_duplicate_variant_results_for_one_stable_identity(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(version1_root, "first.json")
    _write_result(version1_root, "second.json")

    with pytest.raises(DifferentialPairingError, match="duplicate result identity"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_pairs_complete_single_sample_dcdc_and_no_dcdc_sets(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=1)

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert {pair.identity.firmware_role for pair in pairing.pairs} == {"dcdc", "no_dcdc"}
    assert len(pairing.pairs) == 2


def test_aggregates_samples_and_reports_statistics(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    inputs = prepare_comparison_inputs(pairing)
    report = create_diff_comparison_report(pairing, inputs)

    dcdc_pair = next(pair for pair in pairing.pairs if pair.identity.firmware_role == "dcdc")
    dcdc_metrics = {
        metric.variable: metric
        for metric in inputs.metrics
        if metric.identity.firmware_role == "dcdc"
    }
    no_dcdc_metrics = {
        metric.variable: metric
        for metric in inputs.metrics
        if metric.identity.firmware_role == "no_dcdc"
    }
    sleep = dcdc_metrics["sleep_current_ma"]
    avg = dcdc_metrics["avg_current_ma"]
    peak = dcdc_metrics["peak_current_ma"]
    assert dcdc_pair.version1.sample_count == 2
    assert avg.version1_value == pytest.approx(2.5)
    assert avg.version2_value == pytest.approx(2.75)
    assert peak.version1_value == pytest.approx(11.0)
    assert peak.version2_value == pytest.approx(12.1)
    assert sleep.version1_value == pytest.approx(2.0)
    assert sleep.version2_value == pytest.approx(2.2)
    assert sleep.version1_statistics == SampleStatistics(
        count=2,
        mean=2.0,
        median=2.0,
        standard_deviation=pytest.approx(2**0.5),
        minimum=1.0,
        maximum=3.0,
    )

    dcdc_report = next(
        record for record in report["records"]
        if record["identity"]["firmware_role"] == "dcdc"
    )
    sleep_report = next(
        metric for metric in dcdc_report["metrics"]
        if metric["variable"] == "sleep_current_ma"
    )
    assert sleep_report["version1"] == pytest.approx(2.0)
    assert sleep_report["version2"] == pytest.approx(2.2)
    assert sleep_report["percent"] == pytest.approx(10.0)
    assert sleep_report["version1_statistics"] == {
        "count": 2,
        "mean": 2.0,
        "median": 2.0,
        "standard_deviation": pytest.approx(2**0.5),
        "minimum": 1.0,
        "maximum": 3.0,
    }
    assert dcdc_report["version1"]["sample_count"] == 2
    assert set(dcdc_report["version1"]["metrics"]) == {
        "avg_current_ma",
        "peak_current_ma",
        "rx_current_ma",
        "tx_current_ma",
        "sleep_current_ma",
        "event_energy_nj",
        "interval_energy_nj",
    }
    assert dcdc_report["version1"]["metrics"]["rx_current_ma"] == {
        "value": None,
        "available": False,
        "availability_reason": "unavailable",
    }
    assert set(no_dcdc_metrics) == {
        "avg_current_ma",
        "peak_current_ma",
        "rx_current_ma",
        "tx_current_ma",
        "sleep_current_ma",
        "event_energy_nj",
        "interval_energy_nj",
    }
    assert all(
        dcdc_report[variant]["metrics"][variable]["available"] is False
        for variant in ("version1", "version2")
        for variable in ("rx_current_ma", "tx_current_ma")
    )
    assert not any(
        diagnostic["variable"] in {"rx_current_ma", "tx_current_ma"}
        for diagnostic in dcdc_report["diagnostics"]
    )
    no_dcdc_report = next(
        record for record in report["records"]
        if record["identity"]["firmware_role"] == "no_dcdc"
    )
    assert no_dcdc_report["version1"]["metrics"]["rx_current_ma"]["value"] == pytest.approx(3.5)
    assert no_dcdc_report["version2"]["metrics"]["tx_current_ma"]["value"] == pytest.approx(4.95)

    terminal = render_diff_comparison_table(report)
    metric_labels = [
        "avg [mA]",
        "peak [mA]",
        "rx [mA]",
        "tx [mA]",
        "sleep [mA]",
        "event [nJ]",
        "interval [nJ]",
    ]
    assert all(terminal.count(label) == 2 for label in metric_labels)
    assert [terminal.index(label) for label in metric_labels] == sorted(
        terminal.index(label) for label in metric_labels
    )
    assert terminal.count("UNAVAILABLE") == 2

    workbook_path = write_diff_excel_report(report, tmp_path / "sampled-report.xlsx")
    workbook = load_workbook(workbook_path)
    try:
        rows = list(
            workbook["Differential Results"].iter_rows(
                min_row=2, max_col=9, values_only=True
            )
        )
        expected_excel_metrics = [
            "avg [mA]",
            "peak [mA]",
            "rx [mA]",
            "tx [mA]",
            "sleep [mA]",
            "event [nJ]",
            "interval [nJ]",
        ]
        assert len(rows) == 14
        assert [row[2] for row in rows[:7]] == expected_excel_metrics
        assert [row[2] for row in rows[7:]] == expected_excel_metrics
        assert rows[0][1] == "DCDC"
        assert rows[7][1] == "NO_DCDC"
        for row in (rows[2], rows[3]):
            assert row[3:7] == ("N/A", "N/A", "N/A", "N/A")
            assert row[7] == "UNAVAILABLE"
    finally:
        workbook.close()


def test_incomplete_sample_metric_cannot_be_classified_pass(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    for sample_index in (1, 2):
        sample_path = version2_root / f"dcdc_sample_{sample_index:03d}.json"
        sample_payload = json.loads(sample_path.read_text(encoding="utf-8"))
        sample_offset = sample_index - 1
        event = next(
            entry for entry in sample_payload["result_summary"]
            if entry["meas_type"] == "Event"
        )
        interval = next(
            entry for entry in sample_payload["result_summary"]
            if entry["meas_type"] == "Interval"
        )
        sample_payload["result_metric"]["avg_current_ma"] = float(sample_index + 1)
        sample_payload["result_metric"]["peak_current_ma"] = float(sample_index * 2 + 8)
        event["e_mean_nj"] = float(10 + sample_offset * 4)
        interval["e_mean_nj"] = float(20 + sample_offset * 4)
        if sample_index == 2:
            del interval["i_mean_ma"]
        sample_path.write_text(json.dumps(sample_payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    report = create_diff_comparison_report(pairing, prepare_comparison_inputs(pairing))
    dcdc_report = next(
        record for record in report["records"]
        if record["identity"]["firmware_role"] == "dcdc"
    )

    assert dcdc_report["differential_status"] == "INCOMPLETE"
    assert "sleep_current_ma" not in {
        metric["variable"] for metric in dcdc_report["metrics"]
    }
    version2_sleep = dcdc_report["version2"]["metrics"]["sleep_current_ma"]
    assert version2_sleep["available"] is False
    assert version2_sleep["availability_reason"] == "incomplete_sample_metric_set"


def test_sampled_pairing_uses_plan_partition_when_result_partition_is_null(
    tmp_path: Path,
) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for job in plan["jobs"]:
        job["partition"] = "planned-partition"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    for root in (version1_root, version2_root):
        for path in root.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["dataset"]["partition"] = None
            path.write_text(json.dumps(payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert {pair.identity.partition for pair in pairing.pairs} == {"planned-partition"}


def test_rejects_incomplete_sample_set(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    (version2_root / "no_dcdc_sample_002.json").unlink()

    with pytest.raises(
        DifferentialPairingError,
        match="version2 sample set is incomplete.*missing samples \\[2\\]",
    ):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_rejects_duplicate_sample_identity(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    duplicate = version1_root / "duplicate_sample.json"
    duplicate.write_text(
        (version1_root / "dcdc_sample_001.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(DifferentialPairingError, match="duplicate sample 1"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_rejects_inconsistent_sdk_versions_within_one_variant(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    path = version1_root / "dcdc_sample_002.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dataset"]["sdk_version"] = "unexpected-sdk"
    payload["firmware"]["dut_sdk_version"] = "unexpected-sdk"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DifferentialPairingError, match="one consistent SDK version"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_rejects_result_with_wrong_firmware_variant(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    path = version2_root / "dcdc_sample_001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["firmware"]["variant"] = "version1"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DifferentialPairingError, match="expected 'version2'"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


@pytest.mark.parametrize("variant_value", [None, "", "   "], ids=["missing", "empty", "whitespace"])
def test_rejects_result_without_nonempty_firmware_variant(
    tmp_path: Path,
    variant_value: str | None,
) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    path = version2_root / "dcdc_sample_001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if variant_value is None:
        payload["firmware"].pop("variant")
    else:
        payload["firmware"]["variant"] = variant_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DifferentialPairingError, match="no non-empty 'variant' field"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("test_run", "sample_index", 0, "unexpected or missing sample_index"),
        ("dataset", "partition", "unexpected", "partition does not match"),
        ("firmware", "firmware_role", "no_dcdc", "firmware role does not match"),
        ("firmware", "board", "different-board", "board does not match"),
        ("firmware", "device", "EFR32MG24", "disagree on board, device"),
        ("parameters", "supply_voltage_v", 1.55, "supply voltage does not match"),
    ],
)
def test_rejects_inconsistent_sample_identity_and_hardware_metadata(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    message: str,
) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    path = version1_root / "dcdc_sample_001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[section][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DifferentialPairingError, match=message):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_rejects_result_with_unplanned_configuration_hash(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(tmp_path, samples=2)
    path = version1_root / "no_dcdc_sample_001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test_run"]["config_hash"] = "different-config-sha256"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DifferentialPairingError, match="does not match a paired-plan job"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_rejects_sampled_plan_missing_one_dut_mode(tmp_path: Path) -> None:
    version1_root, version2_root, plan_path = _write_sampled_campaign(
        tmp_path,
        samples=2,
        roles=("dcdc",),
    )

    with pytest.raises(DifferentialPairingError, match="include DCDC and NO_DCDC"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_extracts_current_metrics_and_interval_sleep_current() -> None:
    metrics = extract_measurement_metrics(
        {
            "result_metric": {
                "avg_current_ma": 1.25,
                "peak_current_ma": 3,
                "rx_current_ma": 2.5,
                "tx_current_ma": 2.75,
            },
            "result_summary": [
                {"meas_type": "Event", "i_mean_ma": 9.0},
                {"meas_type": "Interval", "i_mean_ma": 0.125},
            ],
        }
    )

    assert metrics.avg_current_ma == 1.25
    assert metrics.peak_current_ma == 3.0
    assert metrics.rx_current_ma == 2.5
    assert metrics.tx_current_ma == 2.75
    assert metrics.sleep_current_ma == 0.125


def test_allows_unavailable_metrics_and_missing_interval_summary() -> None:
    metrics = extract_measurement_metrics(
        {
            "result_metric": {
                "avg_current_ma": 1.25,
                "peak_current_ma": None,
                "rx_current_ma": None,
                "tx_current_ma": None,
            },
            "result_summary": [],
        }
    )

    assert metrics.avg_current_ma == 1.25
    assert metrics.peak_current_ma is None
    assert metrics.rx_current_ma is None
    assert metrics.tx_current_ma is None
    assert metrics.sleep_current_ma is None


def test_rejects_malformed_metric_values_and_ambiguous_interval_summary() -> None:
    with pytest.raises(ResultMetricParseError, match="avg_current_ma"):
        extract_measurement_metrics(
            {
                "result_metric": {
                    "avg_current_ma": "1.25",
                    "peak_current_ma": 2.0,
                    "rx_current_ma": None,
                    "tx_current_ma": None,
                }
            }
        )


def test_summarizes_repeated_metric_values() -> None:
    summary = summarize_sample_values([1, 3, 5])

    assert summary == SampleStatistics(
        count=3,
        mean=3.0,
        median=3.0,
        standard_deviation=2.0,
        minimum=1.0,
        maximum=5.0,
    )


def test_single_sample_has_no_sample_standard_deviation() -> None:
    summary = summarize_sample_values([2.5])

    assert summary == SampleStatistics(
        count=1,
        mean=2.5,
        median=2.5,
        standard_deviation=None,
        minimum=2.5,
        maximum=2.5,
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "at least one value"),
        ([True], "must be numeric"),
        ([1.0, float("nan")], "must be finite"),
        ([float("inf")], "must be finite"),
    ],
)
def test_rejects_empty_or_invalid_sample_values(
    values: list[object], message: str
) -> None:
    with pytest.raises(ResultMetricParseError, match=message):
        summarize_sample_values(values)


def test_prepares_only_available_metrics_and_preserves_json_ready_omission_reasons(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path, rx_value in ((version1_path, 2.0), (version2_path, None)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result_metric"] = {
            "avg_current_ma": 1.0,
            "peak_current_ma": None,
            "rx_current_ma": rx_value,
            "tx_current_ma": 3.0,
            "metric_status": {"rx_current_ma": "unavailable" if rx_value is None else "measured"},
        }
        payload["result_summary"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert [metric.variable for metric in inputs.metrics] == ["avg_current_ma", "tx_current_ma"]
    diagnostics = [diagnostic.to_dict() for diagnostic in inputs.diagnostics]
    assert {item["code"] for item in diagnostics} == {
        "null_metric",
        "marked_unavailable",
        "missing_interval_summary",
    }
    assert all("identity" in item for item in diagnostics)


def test_retains_unmatched_result_diagnostics(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    payload = json.loads(version1_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    version1_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["unmatched_version1_result"]


def test_retains_malformed_result_diagnostics_for_a_matched_pair(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    _write_result(version2_root, "version2.json")
    payload = json.loads(version1_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    version1_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["malformed_result_metric"]


def test_creates_versioned_report_with_percent_statuses_and_absolute_deltas(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path, values in (
        (version1_path, {"avg": 1.0, "peak": 1.0, "rx": 1.0, "tx": 1.0, "sleep": 0.0}),
        (version2_path, {"avg": 1.016, "peak": 1.02, "rx": 0.99, "tx": 1.0, "sleep": 0.0}),
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result_metric"] = {
            "avg_current_ma": values["avg"],
            "peak_current_ma": values["peak"],
            "rx_current_ma": values["rx"],
            "tx_current_ma": values["tx"],
        }
        payload["result_summary"] = [{"meas_type": "Interval", "i_mean_ma": values["sleep"]}]
        path.write_text(json.dumps(payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    report = create_diff_comparison_report(
        pairing,
        prepare_comparison_inputs(pairing),
        thresholds=PercentageThresholdPolicy(warning_percent=1.5, failure_percent=2.0),
    )
    saved_path = write_diff_comparison_report(report, tmp_path / "diff_comparison.json")

    metrics = {item["variable"]: item for item in report["pairs"][0]["metrics"]}
    assert report["schema_version"] == 1
    assert metrics["avg_current_ma"]["delta"] == pytest.approx(0.016)
    assert metrics["avg_current_ma"]["percent"] == pytest.approx(1.6)
    assert metrics["avg_current_ma"]["status"] == "WARNING"
    assert metrics["peak_current_ma"]["status"] == "FAIL"
    assert metrics["rx_current_ma"]["status"] == "PASS"
    assert metrics["sleep_current_ma"]["percent"] is None
    assert metrics["sleep_current_ma"]["status"] == "UNAVAILABLE"
    assert any(item["code"] == "zero_version1_baseline" for item in report["diagnostics"])
    assert json.loads(saved_path.read_text(encoding="utf-8"))["pairs"] == report["pairs"]


def test_renders_requested_markdown_table_from_report_only(tmp_path: Path) -> None:
    report = {
        "pairs": [
            {
                "identity": {
                    "feature": "beacon",
                    "config_path": "tests/configs/ble/beacon/default.json",
                    "firmware_role": "dcdc",
                },
                "version1": {"provenance": {"sisdk_version": "2025.12.3"}},
                "version2": {"provenance": {"sisdk_version": "2025.12.2"}},
                "metrics": [
                    {"variable": "tx_current_ma", "version2": 1.016, "version1": 1.0, "delta": 0.016, "percent": 1.6, "status": "WARNING"},
                    {"variable": "avg_current_ma", "version2": 0.5, "version1": 0.5, "delta": 0.0, "percent": 0.0, "status": "PASS"},
                    {"variable": "sleep_current_ma", "version2": 0.0, "version1": 0.0, "delta": 0.0, "percent": None, "status": "UNAVAILABLE"},
                ],
            }
        ]
    }

    table = render_diff_comparison_table(report)

    assert "SiSDK 2025.12.2" in table
    assert "SiSDK 2025.12.3" in table
    assert "Metric [mA]" in table
    assert "avg" in table
    assert "+0.016" in table
    assert "+1.60%" in table
    assert "! WARN" in table
    assert "peak_current_ma" not in table
    assert table.index("avg") < table.index("tx") < table.index("sleep")
    with pytest.raises(ResultMetricParseError, match="multiple Interval"):
        extract_measurement_metrics(
            {
                "result_metric": {
                    "avg_current_ma": 1.0,
                    "peak_current_ma": 2.0,
                    "rx_current_ma": None,
                    "tx_current_ma": None,
                },
                "result_summary": [
                    {"meas_type": "Interval", "i_mean_ma": 0.1},
                    {"meas_type": "Interval", "i_mean_ma": 0.2},
                ],
            }
        )


def _normalized_report_record(
    feature: str,
    status: str | None,
    *,
    reason: str | None = None,
    firmware_role: str = "dcdc",
    version2_missing: bool = False,
) -> dict:
    version1_value = 1.0
    version2_value = None if version2_missing else (1.02 if status == "FAIL" else 1.01)

    def variant(name: str, value: float | None, *, missing: bool = False) -> dict:
        version = "2026.6.1" if name == "version1" else "2026.6.2"
        build = "101" if name == "version1" else "202"
        avg_available = value is not None
        return {
            "execution_status": "missing_result" if missing else "analyzed",
            "measured_at": f"2026-11-20T10:0{'1' if name == 'version1' else '2'}:00Z",
            "cms_bench_id": f"BENCH-{build}",
            "provenance": {
                "dut_sdk_version": version,
                "dut_sdk_artifactory_build": build,
            },
            "metrics": {
                "avg_current_ma": {
                    "value": value,
                    "available": avg_available,
                    "availability_reason": None if avg_available else "missing_result",
                },
                "peak_current_ma": {
                    "value": None,
                    "available": False,
                    "availability_reason": "not_measured" if not missing else "missing_result",
                },
            },
        }

    version1 = variant("version1", version1_value)
    version2 = variant("version2", version2_value, missing=version2_missing)
    comparisons = []
    if version2_value is not None:
        delta = version2_value - version1_value
        percent = delta / version1_value * 100
        comparisons.append(
            {
                "variable": "avg_current_ma",
                "version1": version1_value,
                "version2": version2_value,
                "delta": delta,
                "percent": percent,
                "status": "FAIL" if percent >= 2 else "PASS",
            }
        )

    return {
        "identity": {
            "feature": feature,
            "config_path": f"tests/configs/ble/{feature}/default.json",
            "config_hash": f"sha-{feature}",
            "partition": firmware_role,
            "mode": "default",
            "board": "BRD4182A",
            "device": "EFR32MG22",
            "firmware_role": firmware_role,
            "supply_voltage_v": 3.3,
        },
        "version1": version1,
        "version2": version2,
        "metrics": comparisons,
        "differential_status": status,
        "reason": reason,
    }


def test_renders_normalized_record_metadata_metrics_and_reasons() -> None:
    report = {
        "records": [
            _normalized_report_record(
                "beacon",
                "FAIL",
                reason="avg_current_ma regression (+2.00%)",
            ),
            _normalized_report_record(
                "connection",
                "INCOMPLETE",
                reason="no version2 result has the same stable identity",
                firmware_role="no_dcdc",
                version2_missing=True,
            ),
        ]
    }

    output = render_diff_comparison_table(report)

    assert "DCDC mode: DCDC" in output
    assert "DCDC mode: NO_DCDC" in output
    assert "Config hash: sha-beacon" in output
    assert "Board: BRD4182A" in output
    assert "SiSDK 2026.6.1" in output
    assert "Build 101" in output
    assert "CMS Bench BENCH-101" in output
    assert "status analyzed" in output
    assert "Differential result: FAIL" in output
    assert "avg_current_ma regression (+2.00%)" in output
    assert "+0.02" in output
    assert "+2.00%" in output
    assert "Differential result: INCOMPLETE" in output
    assert "no version2 result has the same stable identity" in output
    assert "V2: missing_result" in output


def test_renders_final_summary_from_normalized_test_statuses() -> None:
    report = {
        "records": [
            _normalized_report_record("pass_case", "PASS"),
            _normalized_report_record(
                "fail_case", "FAIL", reason="metric regression"
            ),
            _normalized_report_record(
                "incomplete_case",
                "INCOMPLETE",
                reason="missing version2 result",
                version2_missing=True,
            ),
            _normalized_report_record(
                "unknown_case", None, reason="classification not recorded"
            ),
        ]
    }

    output = render_diff_comparison_table(report)

    assert output.endswith(
        "Differential summary: Passed 1 | Failed 1 | Incomplete 2"
    )
    assert "Differential result: INCOMPLETE" in output

    empty_output = render_diff_comparison_table({"records": []})
    assert empty_output == (
        "No comparable metrics.\n\n"
        "Differential summary: Passed 0 | Failed 0 | Incomplete 0"
    )
