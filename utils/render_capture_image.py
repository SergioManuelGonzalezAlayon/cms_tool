from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np


MAX_PLOT_POINTS = 20_000

COLORS = {
    "figure_bg": "#f6f4ef",
    "axes_bg": "#fffdf8",
    "grid": "#ddd5c7",
    "text": "#2f2a24",
    "muted": "#6e665b",
    "current": "#0d6e6e",
    "voltage": "#b45f06",
    "digital": "#7a3e9d",
    "range": "#355c7d",
}


@dataclass(frozen=True)
class SignalSeries:
    name: str
    samples: np.ndarray
    timestamp: np.ndarray


_GPI_PATTERN = re.compile(r"^(gpi|gpio)_(\d+)$")


def load_capture_npz(npz_path: Path) -> dict[str, SignalSeries]:
    signals: dict[str, SignalSeries] = {}
    with np.load(npz_path) as data:
        keys = set(data.files)
        for key in sorted(keys):
            if not key.endswith("__samples"):
                continue

            base_name = key[: -len("__samples")]
            timestamp_key = f"{base_name}__timestamp"
            if timestamp_key not in keys:
                continue

            samples = np.asarray(data[key])
            timestamp = np.asarray(data[timestamp_key])
            if samples.size == 0 or timestamp.size == 0:
                continue

            n = min(samples.size, timestamp.size)
            signals[base_name] = SignalSeries(
                name=base_name,
                samples=samples[:n],
                timestamp=timestamp[:n],
            )

    if not signals:
        raise ValueError(f"No signal pairs found in {npz_path}")

    return signals


def _restore_signal_name(signal_name: str) -> str:
    match = _GPI_PATTERN.match(signal_name)
    if match:
        prefix, index = match.groups()
        return f"{prefix}[{index}]"
    return signal_name


def capture_npz_to_profile(npz_path: Path) -> dict[str, dict[str, np.ndarray]]:
    profile: dict[str, dict[str, np.ndarray]] = {}
    for signal_name, signal in load_capture_npz(npz_path).items():
        profile[_restore_signal_name(signal_name)] = {
            "samples": signal.samples,
            "timestamp": signal.timestamp,
        }
    return profile


def load_result_json(result_json_path: Path) -> dict:
    return json.loads(result_json_path.read_text(encoding="utf-8"))


def selected_plot_window(result_json: dict) -> dict | None:
    selected = result_json.get("test_run", {}).get("selected_plot_window")
    if selected:
        return selected

    windows = result_json.get("test_result_window", [])
    outlier_window = next((row for row in windows if row.get("is_outlier")), None)
    if outlier_window is not None:
        return {
            "selection_reason": "outlier_window",
            "window_index": outlier_window["window_index"],
            "timestamp_s": outlier_window["timestamp_s"],
            "t_duration_us": outlier_window["t_duration_us"],
            "is_outlier": True,
        }
    if windows:
        first_window = windows[0]
        return {
            "selection_reason": "first_available_window",
            "window_index": first_window["window_index"],
            "timestamp_s": first_window["timestamp_s"],
            "t_duration_us": first_window["t_duration_us"],
            "is_outlier": first_window.get("is_outlier", False),
        }
    return None


def slice_signals_to_window(signals: dict[str, SignalSeries], *, start_s: float, duration_us: float) -> dict[str, SignalSeries]:
    end_s = start_s + (duration_us / 1e6)
    sliced: dict[str, SignalSeries] = {}
    for signal_name, signal in signals.items():
        mask = (signal.timestamp >= start_s) & (signal.timestamp <= end_s)
        if not np.any(mask):
            continue

        timestamp = signal.timestamp[mask]
        samples = signal.samples[mask]
        if timestamp.size == 0 or samples.size == 0:
            continue

        sliced[signal_name] = SignalSeries(
            name=signal.name,
            samples=samples,
            timestamp=timestamp - timestamp[0],
        )

    if not sliced:
        raise ValueError("Selected plot window did not overlap any saved signals")

    return sliced


def _display_name(signal_name: str) -> str:
    return signal_name.replace("_", " ").upper()


def _downsample_indices(count: int, max_points: int) -> np.ndarray:
    if count <= max_points:
        return np.arange(count, dtype=int)
    return np.linspace(0, count - 1, num=max_points, dtype=int)


def _downsample_step_indices(samples: np.ndarray, max_points: int) -> np.ndarray:
    count = len(samples)
    if count <= max_points:
        return np.arange(count, dtype=int)

    transition_idx = np.flatnonzero(np.diff(samples) != 0) + 1
    base_idx = np.linspace(0, count - 1, num=max_points // 2, dtype=int)
    idx = np.unique(np.concatenate(([0, count - 1], transition_idx, base_idx)))
    if idx.size > max_points:
        keep = np.linspace(0, idx.size - 1, num=max_points, dtype=int)
        idx = idx[keep]
    return idx


def _signal_duration_s(signal: SignalSeries) -> float:
    if signal.timestamp.size < 2:
        return 0.0
    return float(signal.timestamp[-1] - signal.timestamp[0])


def _format_duration(duration_s: float) -> str:
    if duration_s >= 1:
        return f"{duration_s:.2f} s"
    if duration_s >= 1e-3:
        return f"{duration_s * 1e3:.1f} ms"
    return f"{duration_s * 1e6:.0f} us"


def _plot_analog_signal(ax: plt.Axes, signal: SignalSeries, *, color: str, ylabel: str, scale: float = 1.0) -> None:
    idx = _downsample_indices(len(signal.samples), MAX_PLOT_POINTS)
    ax.plot(signal.timestamp[idx] * 1e3, signal.samples[idx] * scale, color=color, linewidth=1.3)
    ax.set_ylabel(ylabel, color=COLORS["text"])


def _plot_step_signal(ax: plt.Axes, signal: SignalSeries, *, color: str, ylabel: str) -> None:
    idx = _downsample_step_indices(signal.samples, MAX_PLOT_POINTS)
    x_ms = signal.timestamp[idx] * 1e3
    y = signal.samples[idx]
    ax.step(x_ms, y, where="post", color=color, linewidth=1.1)
    ax.set_ylabel(ylabel, color=COLORS["text"])


def render_capture_image(
    npz_path: Path,
    *,
    output_path: Path | None = None,
    result_json_path: Path | None = None,
    dpi: int = 180,
) -> Path:
    signals = load_capture_npz(npz_path)
    window_selection = None
    if result_json_path is not None:
        result_json = load_result_json(result_json_path)
        window_selection = selected_plot_window(result_json)
        if window_selection is not None:
            signals = slice_signals_to_window(
                signals,
                start_s=float(window_selection["timestamp_s"]),
                duration_us=float(window_selection["t_duration_us"]),
            )

    current = signals.get("current")
    if current is None:
        raise ValueError(f"{npz_path} does not contain a 'current' signal")

    analog_signals: list[tuple[SignalSeries, str, str, float]] = [(current, COLORS["current"], "Current (mA)", 1e3)]
    if "voltage" in signals:
        analog_signals.append((signals["voltage"], COLORS["voltage"], "Voltage (V)", 1.0))

    discrete_names = [
        name
        for name in signals
        if name not in {"current", "voltage"} and (name.startswith("gpi_") or name.startswith("gpio_") or name == "current_range")
    ]
    discrete_names.sort()

    height_ratios = [3.2]
    if len(analog_signals) > 1:
        height_ratios.extend([1.5] * (len(analog_signals) - 1))
    height_ratios.extend([0.9] * len(discrete_names))

    fig, axes = plt.subplots(
        nrows=len(height_ratios),
        ncols=1,
        figsize=(14, 2.4 + 1.35 * len(height_ratios)),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios, "hspace": 0.08},
    )
    if not isinstance(axes, np.ndarray):
        axes = np.array([axes])

    fig.patch.set_facecolor(COLORS["figure_bg"])

    for ax in axes:
        ax.set_facecolor(COLORS["axes_bg"])
        ax.grid(True, color=COLORS["grid"], linewidth=0.8, alpha=0.65)
        ax.tick_params(colors=COLORS["muted"], labelsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(COLORS["grid"])
        ax.spines["bottom"].set_color(COLORS["grid"])

    for idx, (signal, color, ylabel, scale) in enumerate(analog_signals):
        _plot_analog_signal(axes[idx], signal, color=color, ylabel=ylabel, scale=scale)

    discrete_offset = len(analog_signals)
    for idx, name in enumerate(discrete_names):
        signal = signals[name]
        color = COLORS["range"] if name == "current_range" else COLORS["digital"]
        _plot_step_signal(axes[discrete_offset + idx], signal, color=color, ylabel=_display_name(name))

    duration_s = _signal_duration_s(current)
    current_ma = current.samples * 1e3
    summary = (
        f"Duration  {_format_duration(duration_s)}\n"
        f"Mean      {float(np.mean(current_ma)):.3f} mA\n"
        f"Peak      {float(np.max(current_ma)):.3f} mA\n"
        f"Samples   {len(current.samples):,}"
    )
    axes[0].text(
        0.995,
        0.97,
        summary,
        transform=axes[0].transAxes,
        ha="right",
        va="top",
        fontsize=9,
        color=COLORS["text"],
        bbox={"boxstyle": "round,pad=0.5", "facecolor": "#f3ede1", "edgecolor": COLORS["grid"]},
    )

    title = npz_path.stem
    subtitle = f"Rendered from {npz_path.name}"
    if window_selection is not None:
        title = f"{npz_path.stem}  window #{int(window_selection['window_index']):03d}"
        subtitle = (
            f"{window_selection['selection_reason'].replace('_', ' ')}"
            f" from {npz_path.name}"
        )

    axes[0].set_title(title, loc="left", fontsize=16, color=COLORS["text"], pad=12, fontweight="bold")
    axes[0].text(
        0.0,
        1.03,
        subtitle,
        transform=axes[0].transAxes,
        ha="left",
        va="bottom",
        fontsize=9,
        color=COLORS["muted"],
    )
    axes[-1].set_xlabel("Time (ms)", color=COLORS["text"])

    output_path = output_path or npz_path.with_suffix(".png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output_path.resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a saved profile NPZ into a local PNG image.",
    )
    parser.add_argument("npz_path", type=Path, help="Path to the capture/profile .npz file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output image path. Defaults to the input file with a .png suffix.",
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        default=None,
        help="Optional result JSON used to slice the saved capture to the selected outlier/reference window.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,
        help="Output image DPI. Defaults to 180.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_path = render_capture_image(
        args.npz_path,
        output_path=args.output,
        result_json_path=args.result_json,
        dpi=args.dpi,
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
