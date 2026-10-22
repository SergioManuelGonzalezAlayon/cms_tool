from collections import deque
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
from threading import Condition, current_thread, get_ident
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys

import numpy as np
import pytest
from pyjoulescope_driver import time64

import joulescope_interface
import instruments.joulescope.read_pyjoulescope_driver as driver_module
import pipeline.streaming as streaming
import results_generator as result_generation
from algorithms.single_pattern_analysis import SinglePatternCurrentProfileAnalyzer
from joulescope_interface import WakePinJoulescope
from pipeline.sequential import AnalysisConfig, OutputConfig, ProfileResult
from results_generator import StreamingProfileConfig


SAMPLE_RATE = 100_000
DEVICE_PATH = "/fake/js320/123"


def _capture_samples():
    low = np.full(100, 0.0001)
    active = np.full(100, 0.001)
    end = np.full(60, 0.0001)
    return np.concatenate((low, active, end, active, end, active, end))


def _analysis_config():
    return AnalysisConfig(
        threshold_on=0.0005,
        threshold_off=0.0005,
        voltage_override=2.4,
        min_thd_duration_start_us=5,
        min_thd_duration_end_us=250,
        min_window_duration_us=500,
        profile_name="streaming_capture",
        unique_first_last_subwindows=True,
        signal_map={
            "current": "current",
            "current_range": "current_range",
            "RX": "gpi[0]",
            "TX": "gpi[1]",
        },
    )


def _disable_wpk(monkeypatch):
    monkeypatch.setattr(joulescope_interface, "_effective_wpk_config", lambda: {})
    monkeypatch.setattr(joulescope_interface, "setVCOM", lambda *_args: None)
    monkeypatch.setattr(joulescope_interface, "setDebugMode", lambda *_args: None)


class _QueuedWakePinJoulescope(WakePinJoulescope):
    """Hardware-free FIFO source that runs the real streaming session."""

    def __init__(self, samples, *, sample_rate=SAMPLE_RATE, chunk_size=37):
        self.use_wake_pin = False
        self.dut_sleep_settle_ms = 500
        self.capture_sequence = 0
        self.capture_data = {"stale": True}
        self.driver = object()
        self.device_paths = [DEVICE_PATH]
        self.signals_full = [
            (DEVICE_PATH, name)
            for name in ("current", "current_range", "gpi[0]", "gpi[1]")
        ]
        self._buffer_condition = Condition()
        self._read_active = False
        self._read_complete = False
        self._fifo_buffers = {}
        self._samples = np.asarray(samples, dtype=np.float64)
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._signal_samples = {
            "current": self._samples,
            "current_range": np.zeros(len(samples), dtype=np.uint8),
            "gpi[0]": np.zeros(len(samples), dtype=np.uint8),
            "gpi[1]": np.zeros(len(samples), dtype=np.uint8),
        }
        self._signal_samples["gpi[0]"][260:360] = 1
        self._signal_samples["gpi[1]"][100:200] = 1
        self.requested_duration = None
        self.requested_sample_count = None
        self.requested_driver = None
        self.requested_signals = None
        self.producer_thread = None
        self.consumer_thread_ids = []

    def _require_driver(self):
        return None

    def pop_buffer(self, signal, wait=False, timeout=None):
        chunk = super().pop_buffer(signal, wait=wait, timeout=timeout)
        if chunk is not None:
            self.consumer_thread_ids.append(get_ident())
        return chunk

    def read_fifo(self, driver, signals, *, duration, on_progress=None):
        self.producer_thread = current_thread()
        self.requested_driver = driver
        self.requested_signals = list(signals)
        self.requested_duration = duration
        self.requested_sample_count = int(round(duration * self._sample_rate))
        with self._buffer_condition:
            self._read_active = True
            self._read_complete = False
            self._fifo_buffers = {}
            for signal in signals:
                _device_path, name = signal
                chunks = deque()
                signal_samples = self._signal_samples[name][:self.requested_sample_count]
                for first in range(0, len(signal_samples), self._chunk_size):
                    chunks.append(
                        {
                            "samples": signal_samples[first:first + self._chunk_size].copy(),
                            "sample_id_start": first,
                            "sample_rate": self._sample_rate,
                            "utc_start": int(first / self._sample_rate * time64.SECOND),
                        }
                    )
                self._fifo_buffers[signal] = chunks
            self._read_active = False
            self._read_complete = True
            self._buffer_condition.notify_all()
        if on_progress is not None:
            on_progress(1.0)



def _record_wpk_and_dut_state(monkeypatch, joulescope):
    operations = []
    monkeypatch.setattr(joulescope_interface, "_effective_wpk_config", lambda: {})
    monkeypatch.setattr(
        joulescope_interface,
        "setVCOM",
        lambda _config, enabled: operations.append(("vcom", enabled)),
    )
    monkeypatch.setattr(
        joulescope_interface,
        "setDebugMode",
        lambda _config, mode: operations.append(("debug", mode)),
    )
    monkeypatch.setattr(
        joulescope_interface,
        "sleep",
        lambda seconds: operations.append(("settle", seconds)),
    )
    joulescope.use_wake_pin = True
    monkeypatch.setattr(joulescope, "sleepDUT", lambda: operations.append("dut_sleep"))
    monkeypatch.setattr(joulescope, "wakeDUT", lambda: operations.append("dut_wake"))
    return operations


def _assert_wpk_and_dut_restored(operations):
    assert operations[0] == "dut_sleep"
    assert operations[-3:] == [("debug", "MCU"), ("vcom", True), "dut_wake"]


def _batch_analyzer(samples):
    timestamps = np.arange(len(samples), dtype=np.float64) / SAMPLE_RATE
    range_samples = np.zeros(len(samples), dtype=np.uint8)
    rx_samples = np.zeros(len(samples), dtype=np.uint8)
    tx_samples = np.zeros(len(samples), dtype=np.uint8)
    rx_samples[260:360] = 1
    tx_samples[100:200] = 1
    analyzer = SinglePatternCurrentProfileAnalyzer(
        signals={
            "current": {"samples": samples, "timestamp": timestamps},
            "current_range": {"samples": range_samples, "timestamp": timestamps},
            "RX": {"samples": rx_samples, "timestamp": timestamps},
            "TX": {"samples": tx_samples, "timestamp": timestamps},
        },
        threshold_on=0.0005,
        threshold_off=0.0005,
        voltage_override=2.4,
        min_thd_duration_start_us=5,
        min_thd_duration_end_us=250,
        min_window_duration_us=500,
        profile_name="streaming_capture",
        unique_first_last_subwindows=True,
    )
    analyzer.process()
    return analyzer



@pytest.fixture
def session_source(monkeypatch, output):
    """Use the real session and shared lifecycle with a hardware-free FIFO."""
    _disable_wpk(monkeypatch)
    driver = MagicMock()
    driver.device_paths.return_value = [DEVICE_PATH]
    monkeypatch.setattr(driver_module, "Driver", lambda: driver)
    data = _QueuedWakePinJoulescope(_capture_samples())
    reads = []

    def read_fifo(instrument, jsdrv, signals, *, duration, on_progress=None):
        for name in ("_samples", "_sample_rate", "_chunk_size", "_signal_samples"):
            setattr(instrument, name, getattr(data, name))
        reads.append((instrument, jsdrv, list(signals), duration))
        _QueuedWakePinJoulescope.read_fifo(
            instrument, jsdrv, signals, duration=duration, on_progress=on_progress,
        )

    monkeypatch.setattr(driver_module.Joulescope, "read_fifo", read_fifo)
    js = WakePinJoulescope(
        signals="i,r,0,1",
        duration=1.0,
        frequency=SAMPLE_RATE,
        open_mode="defaults",
    )
    js.create_driver()
    js.open_devices()
    js.capture_data = {"stale": True}
    js.streaming_profile_config = StreamingProfileConfig(
        analysis=_analysis_config(), output=output,
    )
    try:
        yield js, driver, reads
    finally:
        js.close_driver()


@pytest.fixture
def output(tmp_path):
    return OutputConfig(
        directory=tmp_path,
        consolidated_filename="beacon_default_consolidated_report.csv",
        deep_dive_filename="beacon_default_deep_dive_report.csv",
        html_filename="beacon_default_report.html",
        outlier_plot_filename="beacon_default_profile.png",
        show_reference_title=False,
        include_outliers=False,
        show_thresholds=False,
    )


def test_session_returns_reports_image_and_batch_equivalent_statistics(
    monkeypatch, session_source, output, caplog,
):
    caplog.set_level("INFO")
    js, driver, reads = session_source
    operations = _record_wpk_and_dut_state(monkeypatch, js)
    progress = []
    duration_s = len(_capture_samples()) / SAMPLE_RATE

    result = js.startPowerProfilingSession(
        duration_s, on_progress=progress.append,
    )
    batch = _batch_analyzer(_capture_samples())

    assert isinstance(result, ProfileResult)
    for stats, expected in (
        (result.event_stats, batch.aggr_event_stats),
        (result.interval_stats, batch.aggr_interval_stats),
    ):
        assert stats["current"]["mean"] == pytest.approx(expected["current"]["mean"])
        assert stats["current"]["N"] == expected["current"]["N"]
    assert result.subevent_stats.keys() == batch.aggr_sub_event_stats.keys()
    for path in (
        result.consolidated_csv, result.deep_dive_csv,
        result.html_report, result.outlier_plot,
    ):
        assert path.is_file()
        assert path.stat().st_size > 0
    assert result.outlier_plot.name == output.outlier_plot_filename
    assert result.outlier_plot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result.plot_result["combined_plot_file"] == str(result.outlier_plot)
    assert "Validation streaming artifacts:" in caplog.text
    assert "Validation streaming statistics:" in caplog.text
    assert str(result.outlier_plot) in caplog.text
    instrument, requested_driver, signals, duration = reads[0]
    assert type(instrument) is driver_module.Joulescope
    assert instrument is not js
    assert requested_driver is js.driver is driver
    assert signals == js.signals_full
    assert duration == duration_s
    assert js.duration == 1.0
    assert progress == [1.0]
    assert js.capture_sequence == 1
    assert js.capture_data is None
    assert js.device_paths == [DEVICE_PATH]
    driver.open.assert_called_once_with(DEVICE_PATH, mode="defaults")
    driver.close.assert_not_called()
    driver.finalize.assert_not_called()
    _assert_wpk_and_dut_restored(operations)
    # The existing teardown writer must not rerun batch analysis for this result.
    assert joulescope_interface.write_joulescope_artifacts(
        js, test_name="beacon", config_name="default",
        config_path=Path("tests/configs/ble/beacon/default.json"),
        device_version="js320",
    ) is None


@pytest.mark.parametrize("error", [
    RuntimeError("capture failed in progress"),
    KeyboardInterrupt("stop streaming capture"),
])
def test_session_preserves_error_traceback_and_restores_hardware(
    monkeypatch, session_source, output, caplog, error,
):
    js, driver, reads = session_source
    operations = _record_wpk_and_dut_state(monkeypatch, js)

    def fail_progress(_fraction):
        raise error

    with pytest.raises(type(error)) as raised:
        js.startPowerProfilingSession(
            len(_capture_samples()) / SAMPLE_RATE, on_progress=fail_progress,
        )

    assert raised.value is error
    assert reads
    assert js.capture_sequence == 0
    assert js.capture_data is None
    assert js.driver is driver
    assert js.device_paths == [DEVICE_PATH]
    driver.close.assert_not_called()
    driver.finalize.assert_not_called()
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    _assert_wpk_and_dut_restored(operations)


def test_session_surfaces_reporting_failure_without_a_batch_fallback(
    monkeypatch, session_source, output, caplog,
):
    js, driver, reads = session_source
    operations = _record_wpk_and_dut_state(monkeypatch, js)
    error = ValueError("Validation reporting failed")

    def fail_report(*_args):
        raise error

    monkeypatch.setattr(streaming, "_generate_profile_result", fail_report)
    with pytest.raises(ValueError) as raised:
        js.startPowerProfilingSession(
            len(_capture_samples()) / SAMPLE_RATE,
        )

    assert raised.value is error
    assert reads
    assert js.capture_sequence == 0
    assert js.capture_data is None
    driver.close.assert_not_called()
    driver.finalize.assert_not_called()
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    _assert_wpk_and_dut_restored(operations)


@pytest.fixture
def ble_support(monkeypatch):
    conftest_path = Path(__file__).resolve().parents[1] / "tests" / "conftest.py"
    spec = spec_from_file_location("cms_ble_test_conftest", conftest_path)
    support = module_from_spec(spec)
    spec.loader.exec_module(support)
    # BLE imports its sibling conftest; unit pytest has a different conftest.
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, "conftest", support)
        ble = import_module("ble")
    return ble, support


def _profile_request(test_name, cfg="custom.json", metadata=None):
    options = {"--cfg": cfg, "--cms-fw-metadata": metadata}
    return SimpleNamespace(
        node=SimpleNamespace(name=test_name, nodeid=f"tests/ble.py::{test_name}"),
        config=SimpleNamespace(getoption=options.get),
    )


def _begin_profile_context(monkeypatch, support, request, js, *, config_data=None, upload=False):
    config_data = config_data if config_data is not None else {"hw": {"supply_v": 3.0}}
    request.fixturenames = ["JoulescopeDev", "Config"]
    request.getfixturevalue = {"JoulescopeDev": js, "Config": config_data}.__getitem__
    monkeypatch.setattr(
        support, "_effective_dev_config", lambda _request: {"joulescope": {"model": "js320"}},
    )
    generator = support.ResultGenerator(upload_results_to_database=upload)
    context = support.JoulescopeCaptureContext.__wrapped__(request, generator)
    next(context)
    return context


@pytest.mark.parametrize(("test_name", "expected_duration"), [
    ("test_legacy_beacon", 2.0),
    ("test_beacon", 2.0),
    ("test_legacy_conn_adv", 2.0),
    ("test_conn_adv", 2.0),
    ("test_conn_central", 1.0),
    ("test_conn_peripheral", 1.0),
    ("test_scanner", 1.0),
    ("test_PAwR_per", 2.0),
    ("test_PAwR_obs", 2.0),
])
def test_ble_callers_only_pass_duration_using_existing_capture_context(
    monkeypatch, tmp_path, ble_support, test_name, expected_duration,
):
    from inspect import signature

    ble, support = ble_support
    monkeypatch.chdir(tmp_path)
    advertisement = SimpleNamespace(
        address="test-address", address_type=0, adv_sid=0, periodic_interval=160,
    )
    monkeypatch.setattr(ble, "bgapi_check_event", lambda *_args, **_kwargs: advertisement)
    monkeypatch.setattr(ble, "get_device_addr", lambda _device: "test-address")
    js, dut, tester = MagicMock(), MagicMock(), MagicMock()
    js.capture_sequence = 0
    request = _profile_request(test_name)
    context = _begin_profile_context(monkeypatch, support, request, js)
    profile_config = js.streaming_profile_config
    analysis, output = profile_config.analysis, profile_config.output
    tester.bt.system.get_identity_address.return_value = SimpleNamespace(
        result=0, address="test-address", type=0,
    )
    adv_config = SimpleNamespace(primary_phy=1, secondary_phy=1, phy=1, max_interval=320, set=0)
    values = {
        "JoulescopeDev": js, "DUT": dut, "Tester": tester,
        "DUTAdvConfig": adv_config, "DUTLegAdvConfig": adv_config,
        "TesterAdvConfig": adv_config,
        "DUTScanConfig": MagicMock(return_value=SimpleNamespace(interval=160, phy=1)),
        "ConnectBLEDevices": MagicMock(return_value=(
            None, None, SimpleNamespace(max_interval=80),
        )),
    }
    test = getattr(ble, test_name)
    kwargs = {name: values.get(name, MagicMock()) for name in signature(test).parameters}
    try:
        test(**kwargs)
    finally:
        with pytest.raises(StopIteration):
            next(context)

    assert js.streaming_profile_config is None
    assert "StreamingProfileConfig" not in signature(test).parameters
    js.capture.assert_not_called()
    js.startPowerProfilingSession.assert_called_once_with(expected_duration)
    feature = test_name.removeprefix("test_")
    assert isinstance(profile_config, StreamingProfileConfig)
    assert analysis.voltage_override == 3.0
    assert analysis.threshold_on == analysis.threshold_off == 0.0005
    assert analysis.min_thd_duration_start_us == 5
    assert analysis.min_thd_duration_end_us == 250
    assert analysis.min_window_duration_us == 500
    assert analysis.unique_first_last_subwindows
    assert analysis.profile_name == f"{feature}_custom"
    assert analysis.signal_map["RX"] == "gpi[0]"
    assert analysis.signal_map["TX"] == "gpi[1]"
    assert output.directory == Path("artifacts") / "ble" / feature / "one_factor"
    assert output.consolidated_filename == f"{feature}_custom_consolidated_report.csv"
    assert output.deep_dive_filename == f"{feature}_custom_deep_dive_report.csv"
    assert output.html_filename == f"{feature}_custom_report.html"
    assert output.outlier_plot_filename == f"{feature}_custom_profile.png"
    assert output.plot_outliers
    assert not output.show_reference_title
    assert not output.include_outliers
    assert not output.show_thresholds


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize(("cfg", "relative_directory"), [
    (None, "default"),
    ("custom.json", "one_factor"),
    ("full_sweep/partition-01/case.json", "full_sweep/partition-01"),
])
def test_result_context_selects_local_or_per_run_output(
    monkeypatch, tmp_path, ble_support, remote, cfg, relative_directory,
):
    _, support = ble_support
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "ram-runtime" / "measurement-123"
    metadata = str(runtime / "cms_fw_metadata.json") if remote else None
    request = _profile_request("test_beacon", cfg=cfg, metadata=metadata)
    js = SimpleNamespace(capture_sequence=0)
    context = _begin_profile_context(monkeypatch, support, request, js)
    profile_config = js.streaming_profile_config
    with pytest.raises(StopIteration):
        next(context)

    root = runtime / "streaming_artifacts" if remote else Path("artifacts")
    assert profile_config.output.directory == root / "ble" / "beacon" / relative_directory
    assert profile_config.output.directory.is_dir()
    stem = f"beacon_{Path(cfg or 'default.json').stem}"
    assert profile_config.analysis.profile_name == stem
    assert profile_config.output.outlier_plot_filename == f"{stem}_profile.png"
    assert js.streaming_profile_config is None


def _prepare_streaming_json_context(monkeypatch, tmp_path, support, js, *, remote=True, upload=False):
    monkeypatch.chdir(tmp_path)
    config_data = {
        "hw": {"supply_v": 3.0},
        "bench": {"bench_id": "software-bench"},
        "firmware": {
            "dut_slcp": "apps/ncp_ble_dut_dcdc.slcp", "board": "brd-test",
            "board_revision": "a01", "part_number": "test-part", "serial_number": "123",
            "sisdk_version": "test-sdk", "sisdk_name": "test-sdk-name",
        },
        "advertiser": {"interval": 200, "phy": "1m", "payload": 18},
    }
    config_path = Path("tests/configs/ble/beacon/default.json")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    metadata = str(tmp_path / "runtime/measurement-123/cms_fw_metadata.json") if remote else None
    request = _profile_request("test_beacon", cfg=None, metadata=metadata)
    context = _begin_profile_context(
        monkeypatch, support, request, js, config_data=config_data, upload=upload,
    )
    return context, Path(js.streaming_profile_config.output.directory)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("upload", [False, True])
def test_successful_session_teardown_writes_cms_result_json(
    monkeypatch, tmp_path, ble_support, session_source, remote, upload,
):
    _, support = ble_support
    js, _driver, _reads = session_source
    context, out_dir = _prepare_streaming_json_context(
        monkeypatch, tmp_path, support, js, remote=remote, upload=upload,
    )
    uploads = []
    monkeypatch.setattr(
        result_generation, "upload_result_json_to_database",
        lambda path, **_kwargs: uploads.append(("json", path)),
    )
    monkeypatch.setattr(
        result_generation, "upload_result_file_to_database",
        lambda path, **_kwargs: uploads.append(("png", path)),
    )

    def forbid_batch_or_npz(*_args, **_kwargs):
        raise AssertionError("Streaming JSON must not regenerate a batch profile or NPZ")

    monkeypatch.setattr(support, "write_joulescope_artifacts", forbid_batch_or_npz)
    monkeypatch.setattr(result_generation, "_save_capture_npz", forbid_batch_or_npz)
    result = js.startPowerProfilingSession(len(_capture_samples()) / SAMPLE_RATE)
    result_json = out_dir / "beacon_default_result.json"
    assert not result_json.exists()
    assert js.streaming_result is result
    with pytest.raises(StopIteration):
        next(context)

    payload = json.loads(result_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["result_type"] == "current_measurement"
    assert payload["test_run"]["test_name"] == "beacon"
    assert payload["test_run"]["config_name"] == "default"
    assert payload["test_run"]["status"] == "analyzed"
    assert payload["test_run"]["capture_retained"] is False
    assert payload["test_run"]["capture_policy"]["retention_status"] == "unavailable_from_streaming_result"
    assert payload["dataset"]["sdk_version"] == "test-sdk"
    assert payload["storage"]["cms_bench_id"] == "software-bench"
    metrics = payload["result_metric"]
    assert metrics["avg_current_ma"] is None
    assert metrics["peak_current_ma"] is None
    assert metrics["metric_status"]["avg_current_ma"] == "unavailable_from_streaming_result"
    assert metrics["metric_status"]["peak_current_ma"] == "unavailable_from_streaming_result"
    assert metrics["rx_current_ma"] is not None
    assert metrics["tx_current_ma"] is not None
    event_summary = next(row for row in payload["result_summary"] if row["meas_type"] == "Event")
    interval_summary = next(row for row in payload["result_summary"] if row["meas_type"] == "Interval")
    assert event_summary["count"] == result.event_stats["current"]["N"] == 2
    assert event_summary["i_mean_ma"] == pytest.approx(result.event_stats["current"]["mean"])
    assert interval_summary["i_mean_ma"] == pytest.approx(result.interval_stats["current"]["mean"])
    windows = payload["test_result_window"]
    assert [row["window_index"] for row in windows] == [1, 2]
    assert all(row["profile_type"] == "event" for row in windows)
    assert windows[0]["i_window_mean_ma"] == pytest.approx(event_summary["i_mean_ma"])
    assert windows[0]["t_duration_us"] == pytest.approx(event_summary["t_mean_us"], abs=0.001)
    assert windows[0]["subwindow_stats_json"][0]["max_current"] == 0.001
    artifacts = {ref["artifact_type"]: ref for ref in payload["artifact_ref"]}
    assert set(artifacts) == {"config_snapshot", "consolidated_csv", "deep_dive_csv", "html_report", "analysis_png"}
    assert artifacts["analysis_png"]["path"] == result.outlier_plot.as_posix()
    assert all(len(ref["sha256"]) == 64 for ref in artifacts.values())
    assert not list(out_dir.glob("*.npz"))
    assert uploads == ([("json", result_json), ("png", result.outlier_plot)] if upload else [])
    status = json.loads((out_dir / "beacon_default_upload_status.json").read_text(encoding="utf-8"))
    assert status["status"] == ("uploaded" if upload else "disabled")
    assert status["phase"] == "complete"
    assert status["png_path"] == result.outlier_plot.as_posix()
    assert js.streaming_profile_config is None
    assert js.streaming_result is None


@pytest.mark.parametrize("failure", ["report", "upload_json", "upload_png"])
def test_streaming_json_failure_propagates_and_records_status(
    monkeypatch, tmp_path, ble_support, session_source, failure,
):
    _, support = ble_support
    js, _driver, _reads = session_source
    context, out_dir = _prepare_streaming_json_context(
        monkeypatch, tmp_path, support, js, upload=failure != "report",
    )
    result = js.startPowerProfilingSession(len(_capture_samples()) / SAMPLE_RATE)

    def fail_upload(*_args, **_kwargs):
        raise RuntimeError("simulated upload failure")

    monkeypatch.setattr(result_generation, "upload_result_json_to_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(result_generation, "upload_result_file_to_database", lambda *_args, **_kwargs: None)
    if failure == "report":
        text = result.deep_dive_csv.read_text(encoding="utf-8")
        result.deep_dive_csv.write_text(text.replace("False", "invalid", 1), encoding="utf-8")
        expected_error, match = ValueError, "Invalid deep-dive is_outlier"
    else:
        target = "upload_result_json_to_database" if failure == "upload_json" else "upload_result_file_to_database"
        monkeypatch.setattr(result_generation, target, fail_upload)
        expected_error, match = RuntimeError, "simulated upload failure"
    with pytest.raises(expected_error, match=match):
        next(context)

    status = json.loads((out_dir / "beacon_default_upload_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["phase"] == ("result_json" if failure == "report" else failure)
    assert match in status["error"]
    assert (out_dir / "beacon_default_result.json").exists() is (failure != "report")
    assert js.streaming_profile_config is None
    assert js.streaming_result is None


def test_session_requires_context_before_acquisition(session_source):
    js, driver, reads = session_source
    js.streaming_profile_config = None
    with pytest.raises(RuntimeError, match="context is not configured"):
        js.startPowerProfilingSession(0.01)
    assert reads == []
    assert js.capture_sequence == 0
    driver.close.assert_not_called()
    driver.finalize.assert_not_called()
