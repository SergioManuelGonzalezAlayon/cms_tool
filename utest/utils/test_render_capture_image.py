import json

import numpy as np
import pytest

from utils.render_capture_image import (
    capture_npz_to_profile,
    load_capture_npz,
    render_capture_image,
    selected_plot_window,
)


@pytest.fixture
def capture_path(tmp_path):
    capture_path = tmp_path / "capture.npz"
    timestamps = np.array([0.0, 0.0005, 0.001, 0.0015])
    np.savez(
        capture_path,
        current__samples=np.array([0.001, 0.002, 0.003, 0.004]),
        current__timestamp=timestamps,
        gpi_0__samples=np.array([0, 1, 1, 0]),
        gpi_0__timestamp=timestamps,
    )
    return capture_path


def test_load_capture_npz_reads_expected_signals(capture_path):
    signals = load_capture_npz(capture_path)

    assert "current" in signals
    assert "gpi_0" in signals
    assert signals["current"].samples.size > 0
    assert signals["current"].timestamp.size == signals["current"].samples.size


def test_capture_npz_to_profile_restores_gpio_names(capture_path):
    profile = capture_npz_to_profile(capture_path)

    assert "current" in profile
    assert "gpi[0]" in profile
    assert profile["current"]["samples"].size > 0
    assert profile["current"]["timestamp"].size == profile["current"]["samples"].size


def test_render_capture_image_writes_png(capture_path, tmp_path):
    output_path = tmp_path / "capture_preview.png"

    written_path = render_capture_image(capture_path, output_path=output_path)

    assert written_path == output_path.resolve()
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_selected_plot_window_prefers_explicit_json_selection():
    result_json = {
        "test_run": {
            "selected_plot_window": {
                "selection_reason": "outlier_window",
                "window_index": 3,
                "timestamp_s": 0.048827,
                "t_duration_us": 3477.0,
                "is_outlier": True,
            }
        },
        "test_result_window": [],
    }

    assert selected_plot_window(result_json) == result_json["test_run"]["selected_plot_window"]


def test_render_capture_image_can_slice_using_result_json(capture_path, tmp_path):
    output_path = tmp_path / "capture_window.png"
    result_json_path = tmp_path / "result.json"
    result_json_path.write_text(
        json.dumps(
            {
                "test_run": {
                    "selected_plot_window": {
                        "selection_reason": "reference_window",
                        "window_index": 1,
                        "timestamp_s": 0.0,
                        "t_duration_us": 1000.0,
                        "is_outlier": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    written_path = render_capture_image(
        capture_path,
        output_path=output_path,
        result_json_path=result_json_path,
    )

    assert written_path == output_path.resolve()
    assert output_path.exists()
    assert output_path.stat().st_size > 0
