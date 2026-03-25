import pytest, logging
from dataclasses import dataclass, asdict
import json
from instruments.joulescope.read_pyjoulescope_driver import Joulescope, get_profile
from algorithms.single_pattern_analysis import SinglePatternCurrentProfileAnalyzer
from presentation.html_report import generate_report
import matplotlib.pyplot as plt
from pathlib import Path

log = logging.getLogger(__name__)

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


def savePlotData(profile, data_type, title, ylabel, out_dir, test_name=None, config_name=None):
    signal_name = title.replace(" ", "").lower()
    if test_name and config_name:
        filename = f"{_safe_name(test_name)}_{_safe_name(config_name)}_{signal_name}.png"
    else:
        filename = "result_" + signal_name + ".png"

    plt.figure()
    plt.plot(profile[data_type]["timestamp"],
             profile[data_type]["samples"])
    plt.title(title)
    plt.xlabel("Time (s)")
    plt.ylabel(ylabel)
    plt.savefig(out_dir / filename , dpi=149, bbox_inches="tight")
    plt.close()


def _artifact_stem(test_name=None, config_name=None):
    if test_name and config_name:
        return f"{_safe_name(test_name)}_{_safe_name(config_name).removesuffix('_json')}"
    if test_name:
        return _safe_name(test_name)
    return "result"


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

@dataclass
class JoulescopeConfig:
    signals: str = "i,r,0,1"
    duration: str | float = 1
    frequency: int = 1_000_000
    serial_number: str | None = None
    open_mode: str = "defaults"
    vref: str = "3v3"
    post_process: str = "profile"
    irange: str = "auto"
    verbose: bool = False


@pytest.fixture(autouse=True)
def JoulescopeCaptureContext(request: pytest.FixtureRequest):
    if "JoulescopeDev" not in request.fixturenames:
        yield
        return

    js = request.getfixturevalue("JoulescopeDev")
    js.capture_test_name = request.node.name.removeprefix("test_")
    cfg_opt = request.config.getoption("--cfg")
    js.capture_config_name = Path(cfg_opt).name if cfg_opt else "default.json"
    if "Config" in request.fixturenames:
        config = request.getfixturevalue("Config")
        supply_v = config.get("hw", {}).get("supply_v")
        js.capture_supply_v = supply_v
        if supply_v is not None:
            js.vref = supply_v
            if getattr(js, "driver", None) is not None:
                for device_path in getattr(js, "device_paths", []):
                    js.driver.publish(f"{device_path}/c/gpio/vref", js.vref)
    yield


@pytest.fixture(scope="session")
def JoulescopeDev():
    log.info("Enabling Joulescope")
    cfg = JoulescopeConfig(open_mode="restore")
    js = Joulescope(**asdict(cfg))

    js.create_driver()
    js.open_devices()

    yield js

    # Save the capture data
    if getattr(js, "capture_data", None) is not None:
        log.info("Saving captured plots and analysis outputs")
        profile = get_profile(js, js.capture_data)

        out_dir = Path("artifacts")
        out_dir.mkdir(exist_ok=True)
        test_name = getattr(js, "capture_test_name", None)
        config_name = getattr(js, "capture_config_name", None)

        if "current" in profile:
            savePlotData(profile, "current", "Current", "mA", out_dir, test_name, config_name)

        if "gpi[0]" in profile:
            savePlotData(profile, "gpi[0]", "GPIO 0", "Level", out_dir, test_name, config_name)

        if "gpi[1]" in profile:
            savePlotData(profile, "gpi[1]", "GPIO 1", "Level", out_dir, test_name, config_name)

        result_paths = write_single_pattern_results(
            profile,
            out_dir,
            test_name=test_name,
            config_name=config_name,
            voltage_override=getattr(js, "capture_supply_v", None),
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

    js.close_driver()

def test_capture(JoulescopeDev):
    """
    Test the Joulescope basic capture 
    """
    #JouleIscopeDev.run()
    pass

def test_GPIOInCheck():
    NotImplementedError

def test_GPIOOutCheck():
    NotImplementedError
