import pytest, logging
from dataclasses import dataclass, asdict
import json
import subprocess
from instruments.joulescope.read_pyjoulescope_driver import Joulescope, get_profile
from algorithms.single_pattern_analysis import SinglePatternCurrentProfileAnalyzer
from presentation.html_report import generate_report
from presentation.outlier_plots import OutlierPlotsGenerator
from pathlib import Path
from time import sleep
from utils.config_loader import effective_dev_config
from utils.wpk import setDebugMode, setVCOM

log = logging.getLogger(__name__)
DUT_WAKE_GPIO = 0
USB_RESET_TIMEOUT_S = 15.0
_CMS_RUNTIME_CONFIG_PATH = None
_CMS_FW_METADATA_PATH = None

def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)


def build_single_pattern_signals(profile: dict, *, gpio_label_map: dict[str, str] | None = None) -> dict:
    gpio_label_map = gpio_label_map or {}
    signals: dict = {}

    if "current" not in profile:
        raise ValueError("profile must include 'current'")

    signals["current"] = profile["current"]

    if "voltage" in profile:
        signals["voltage"] = profile["voltage"]

    if "current_range" in profile:
        signals["current_range"] = profile["current_range"]
    elif "range" in profile:
        signals["current_range"] = profile["range"]

    for source_name in ("gpi[0]", "gpio[0]", "gpi[1]", "gpio[1]", "gpi[2]", "gpio[2]", "gpi[3]", "gpio[3]"):
        if source_name not in profile:
            continue
        target_name = gpio_label_map.get(source_name, source_name.upper())
        signals[target_name] = profile[source_name]

    return signals


def build_single_pattern_analyzer(
    profile: dict,
    *,
    gpio_label_map: dict[str, str] | None = None,
    voltage_override: float | None = None,
    threshold_on: float = 0.0005,
    threshold_off: float = 0.0005,
    min_thd_duration_start_us: int = 5,
    min_thd_duration_end_us: int = 250,
    min_window_duration_us: int = 500,
    profile_name: str = "current_profile",
    unique_subwindow_labels: bool = False,
    unique_first_last_subwindows: bool = True,
    zscore_threshold: float = 3,
) -> SinglePatternCurrentProfileAnalyzer:
    signals = build_single_pattern_signals(profile, gpio_label_map=gpio_label_map)
    return SinglePatternCurrentProfileAnalyzer(
        signals=signals,
        voltage_override=voltage_override,
        threshold_on=threshold_on,
        threshold_off=threshold_off,
        min_thd_duration_start_us=min_thd_duration_start_us,
        min_thd_duration_end_us=min_thd_duration_end_us,
        min_window_duration_us=min_window_duration_us,
        profile_name=profile_name,
        unique_subwindow_labels=unique_subwindow_labels,
        unique_first_last_subwindows=unique_first_last_subwindows,
        zscore_threshold=zscore_threshold,
    )


class WakePinJoulescope(Joulescope):
    """Local Joulescope policy for DUT wake GPIO semantics."""

    def __init__(
        self,
        *args,
        use_wake_pin: bool = False,
        dut_sleep_settle_ms: float = 500,
        dut_wake_gpio: int = DUT_WAKE_GPIO,
        dut_awake_settle_s: float = 0.01,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.use_wake_pin = use_wake_pin
        self.dut_sleep_settle_ms = dut_sleep_settle_ms
        self.dut_wake_gpio = dut_wake_gpio
        self.dut_awake_settle_s = dut_awake_settle_s
        self.capture_sequence = 0

    def open_devices(self):
        device_paths = super().open_devices()
        if self.use_wake_pin:
            self.wakeDUT()
        return device_paths

    def close_driver(self):
        if self.driver is None:
            return
        if self.use_wake_pin and self.device_paths:
            try:
                self.sleepDUT()
            except Exception:
                pass
        super().close_driver()

    def capture(self, seconds: float, on_progress=None):
        if not self.use_wake_pin:
            result = _capture_with_wpk_quieting(
                lambda: super(WakePinJoulescope, self).capture(
                    seconds,
                    on_progress=on_progress,
                )
            )
            self.capture_sequence += 1
            return result

        self._require_driver()
        if not self.signals_full:
            raise RuntimeError("signals_full not set; did you open devices first?")

        self.sleepDUT()
        try:
            sleep(self.dut_sleep_settle_ms / 1000.0)
            self.capture_data = _capture_with_wpk_quieting(
                lambda: self.read(
                    self.driver,
                    self.signals_full,
                    duration=float(seconds),
                    on_progress=on_progress,
                )
            )
        finally:
            self.wakeDUT()
        self.capture_sequence += 1
        return self.capture_data

    def wakeDUT(self):
        """Assert the DUT wake line."""
        self.gpo_clear(self.dut_wake_gpio)

    def sleepDUT(self):
        """Release the DUT wake line to the sleep state."""
        self.gpo_set(self.dut_wake_gpio)

    def setDUTAwake(self, maybe=None):
        """
        Ensure DUT is permanently awake:
        - force known HIGH state
        - generate clean falling edge
        - hold LOW afterwards
        """
        device_path = self._get_open_device_path()

        # Preserve the known-good wake pulse sequence for this bench.
        self.driver.publish(f"{device_path}/s/gpo/+/!set", 1 << self.dut_wake_gpio)
        sleep(self.dut_awake_settle_s)
        self.driver.publish(f"{device_path}/s/gpo/+/!clr", 1 << self.dut_wake_gpio)
        sleep(self.dut_awake_settle_s)
        self.driver.publish(f"{device_path}/s/gpo/+/!clr", 1 << self.dut_wake_gpio)


def _configure_wpk_config_paths(runtime_path=None, metadata_path=None) -> None:
    global _CMS_RUNTIME_CONFIG_PATH, _CMS_FW_METADATA_PATH
    _CMS_RUNTIME_CONFIG_PATH = runtime_path
    _CMS_FW_METADATA_PATH = metadata_path


def _effective_wpk_config() -> dict:
    return effective_dev_config(
        runtime_path=_CMS_RUNTIME_CONFIG_PATH,
        metadata_path=_CMS_FW_METADATA_PATH,
    )


def _capture_with_wpk_quieting(capture):
    config = _effective_wpk_config()
    setVCOM(config, False)
    try:
        setDebugMode(config, "OFF")
        try:
            result = capture()
            log.info("Joulescope capture complete")
            return result
        finally:
            setDebugMode(config, "MCU")
    finally:
        setVCOM(config, True)


def _artifact_stem(test_name=None, config_name=None):
    if test_name and config_name:
        return f"{_safe_name(test_name)}_{_safe_name(config_name).removesuffix('_json')}"
    if test_name:
        return _safe_name(test_name)
    return "result"


def _artifact_out_dir(config_path: Path) -> Path:
    configs_root = (Path("tests") / "configs").resolve()
    resolved = config_path.resolve()
    try:
        relative = resolved.relative_to(configs_root)
    except ValueError:
        out_dir = Path("artifacts")
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    if len(relative.parts) < 3:
        out_dir = Path("artifacts")
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    suite = relative.parts[0]
    feature = relative.parts[1]
    if relative.parts[2] == "default.json":
        out_dir = Path("artifacts") / suite / feature / "default"
    elif relative.parts[2] == "full_sweep":
        partition = relative.parts[3] if len(relative.parts) > 3 else "unpartitioned"
        out_dir = Path("artifacts") / suite / feature / "full_sweep" / partition
    else:
        out_dir = Path("artifacts") / suite / feature / "one_factor"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def write_single_pattern_results(profile, out_dir, *, test_name=None, config_name=None, voltage_override=None):
    stem = _artifact_stem(test_name, config_name)
    analyzer = build_single_pattern_analyzer(
        profile,
        gpio_label_map={
            "gpi[0]": "RX",
            "gpi[1]": "TX",
            "gpio[0]": "RX",
            "gpio[1]": "TX",
        },
        voltage_override=voltage_override,
        profile_name=stem,
        unique_first_last_subwindows=True,
    )
    try:
        analyzer.process()
    except ValueError as exc:
        note_path = out_dir / f"{stem}_analysis_note.json"
        note_path.write_text(
            json.dumps(
                {
                    "status": "no_windows_detected",
                    "reason": str(exc),
                    "profile_name": stem,
                    "threshold_on": analyzer.threshold_on,
                    "threshold_off": analyzer.threshold_off,
                    "voltage_override": analyzer.voltage_override,
                    "available_signals": sorted(profile.keys()),
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return {
            "analyzer": analyzer,
            "consolidated_csv": None,
            "deep_dive_csv": None,
            "html_report": None,
            "note_path": note_path,
        }


    consolidated_csv = analyzer.dump_consolidated_csv_report(
        out_dir,
        filename=f"{stem}_consolidated_report.csv",
    )
    deep_dive_csv = analyzer.dump_deep_dive_csv_report(
        out_dir,
        filename=f"{stem}_deep_dive_report.csv",
    )
    html_report = out_dir / f"{stem}_report.html"
    generate_report(consolidated_csv, deep_dive_csv, str(html_report))

    return {
        "analyzer": analyzer,
        "consolidated_csv": Path(consolidated_csv),
        "deep_dive_csv": Path(deep_dive_csv),
        "html_report": html_report,
        "note_path": None,
    }


def write_joulescope_artifacts(
    js: Joulescope,
    *,
    test_name: str,
    config_name: str,
    config_path: Path,
    device_version: str,
    voltage_override: float | None = None,
    result_generator=None,
):
    if getattr(js, "capture_data", None) is None:
        return None

    log.info("Saving analysis outputs for %s/%s", test_name, config_name)
    profile = get_profile(js, js.capture_data, device_version)

    out_dir = _artifact_out_dir(config_path)

    result_paths = write_single_pattern_results(
        profile,
        out_dir,
        test_name=test_name,
        config_name=config_name,
        voltage_override=voltage_override,
    )
    if result_paths["note_path"] is not None:
        log.warning("Analyzer did not detect windows yet; note saved to %s", result_paths["note_path"])
    else:
        log.info(
            "Artifacts saved: consolidated=%s deep_dive=%s html=%s",
            result_paths["consolidated_csv"],
            result_paths["deep_dive_csv"],
            result_paths["html_report"],
        )

    result_json_path = None
    if result_generator is not None:
        result_json_path = result_generator.write(
            profile=profile,
            out_dir=out_dir,
            artifact_stem=_artifact_stem(test_name, config_name),
            result_paths=result_paths,
        )
        log.info("Result JSON saved: %s", result_json_path)

    return {
        "profile": profile,
        "out_dir": out_dir,
        "result_paths": result_paths,
        "result_json_path": result_json_path,
    }

@dataclass
class JoulescopeConfig:
    signals: str = "i,r,0,1"
    duration: str | float = 1
    frequency: int = 1_000_000
    open_mode: str = "defaults"
    vref: str = "3v3"
    post_process: str = "profile"
    irange: str = "auto"
    use_wake_pin: bool = True
    dut_sleep_settle_ms: float = 500
    dut_wake_gpio: int = DUT_WAKE_GPIO
    dut_awake_settle_s: float = 0.01
    verbose: bool = False

@pytest.fixture(scope="session")
def JoulescopeUsbReset(request: pytest.FixtureRequest):
    cfg = effective_dev_config(
        runtime_path=request.config.getoption("--cms-runtime-config"),
        metadata_path=request.config.getoption("--cms-fw-metadata"),
    )
    usb_id = cfg.get("joulescope", {}).get("id")

    if not usb_id:
        log.warning("SETUP SKIP: Joulescope USB reset; no joulescope.id configured")
        return

    _reset_joulescope_usb(usb_id)


def _reset_joulescope_usb(usb_id: str) -> None:
    # Reset the JS USB to avoid the USB driver deaklocking the whole test run
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            log.info(
                "SETUP START: Joulescope USB reset id=%s attempt=%d/%d",
                usb_id,
                attempt,
                attempts,
            )
            result = subprocess.run(
                ["usbreset", usb_id],
                capture_output=True,
                text=True,
                check=True,
                timeout=USB_RESET_TIMEOUT_S,
            )
            log.info(
                "SETUP COMPLETE: Joulescope USB reset id=%s output=%s",
                usb_id,
                result.stdout.strip(),
            )
            break

        except subprocess.TimeoutExpired as e:
            log.error(
                "SETUP TIMEOUT: Joulescope USB reset id=%s timeout_s=%s",
                usb_id,
                USB_RESET_TIMEOUT_S,
            )
            raise RuntimeError(
                f"Joulescope USB reset timed out after {USB_RESET_TIMEOUT_S:g} seconds"
            ) from e
        except subprocess.CalledProcessError as e:
            log.error(
                "usbreset failed: rc=%d stdout=%r stderr=%r",
                e.returncode,
                e.stdout,
                e.stderr,
            )
            if attempts == attempt:
                raise
        sleep(0.5)

@pytest.fixture(scope="session")
def JoulescopeDev(JoulescopeUsbReset, request: pytest.FixtureRequest):
    log.info("SETUP START: Joulescope driver creation")
    cfg = JoulescopeConfig(open_mode="defaults")
    _configure_wpk_config_paths(
        runtime_path=request.config.getoption("--cms-runtime-config"),
        metadata_path=request.config.getoption("--cms-fw-metadata"),
    )
    js = WakePinJoulescope(**asdict(cfg))

    js.create_driver()
    log.info("SETUP COMPLETE: Joulescope driver creation")
    log.info("SETUP START: Joulescope device open")
    js.open_devices()
    log.info("SETUP COMPLETE: Joulescope device open")

    yield js

    js.close_driver()
