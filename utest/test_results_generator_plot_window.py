from pathlib import Path
import importlib.util


_RESULTS_GENERATOR_PATH = Path(__file__).resolve().parents[1] / "tests" / "results_generator.py"
_SPEC = importlib.util.spec_from_file_location("results_generator_local", _RESULTS_GENERATOR_PATH)
assert _SPEC is not None and _SPEC.loader is not None
results_generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(results_generator)


def test_select_plot_window_prefers_outlier_window():
    window_rows = [
        {"window_index": 1, "timestamp_s": 0.010, "t_duration_us": 1000, "is_outlier": False},
        {"window_index": 2, "timestamp_s": 0.020, "t_duration_us": 1200, "is_outlier": True},
    ]

    selected = results_generator._select_plot_window(window_rows)

    assert selected == {
        "selection_reason": "outlier_window",
        "window_index": 2,
        "timestamp_s": 0.020,
        "t_duration_us": 1200,
        "is_outlier": True,
    }


def test_select_plot_window_uses_reference_index_when_no_outlier():
    window_rows = [
        {"window_index": 1, "timestamp_s": 0.010, "t_duration_us": 1000, "is_outlier": False},
        {"window_index": 2, "timestamp_s": 0.020, "t_duration_us": 1200, "is_outlier": False},
    ]

    selected = results_generator._select_plot_window(
        window_rows,
        {"reference_window_index": 1},
    )

    assert selected == {
        "selection_reason": "reference_window",
        "window_index": 2,
        "timestamp_s": 0.020,
        "t_duration_us": 1200,
        "is_outlier": False,
    }
