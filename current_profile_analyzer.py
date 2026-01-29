

import numpy as np
import os
import sys
sys.path.append(os.path.dirname(__file__))
from read_pyjoulescope_driver import Joulescope
import logging
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

class CurrentProfileAnalyzer:
    """
    Analyzes current/voltage profiles and digital events from a dictionary of signals

    Attributes (constructor arguments):
      - signals (dict): Dictionary mapping signal names (e.g., 'current', 'voltage', 'gpi[0]') to dicts with 'samples' (np.ndarray) and 'timestamp' (np.ndarray).
      - threshold_on (float): Current threshold (A) to detect window/event start.
      - threshold_off (float): Current threshold (A) to detect window/event end.
      - voltage_override (float, optional): If set, overrides measured voltage for energy calculations.
      - min_thd_duration_start_us (int, optional): Minimum duration (us) above threshold_on to qualify as window start.
      - min_thd_duration_end_us (int, optional): Minimum duration (us) below threshold_off to qualify as window end.
      - default_subwindow_label (str, optional): Default label for sub-windows (default: 'EM0/1').
      - min_window_duration_us (int, optional): Minimum window duration (us) to keep.
      - profile_name (str, optional): Name for plots and reports.
    
    Methods (main methods and attributes):
      - process(): Runs the full analysis pipeline (window finding, statistics, etc.).
      - plot_windows(): Plots current, voltage, and event windows.
      - plot_statistics(): Plots histograms of event/interval/sub-event statistics.
      - plot_avg_current_profile(): Plots the average current profile across all windows.
      - print_summary(), print_statistics(): Print human-readable summaries/statistics.
      - aggr_event_stats (dict): Aggregated statistics for detected windows/events.
      - aggr_sub_event_stats (dict): Aggregated statistics for sub-windows (by label).
      - aggr_interval_stats (dict): Aggregated statistics for intervals between windows.
      - window_stats, sub_window_stats, interval_stats (list): Per-window/sub-window/interval statistics.
    
    Example usage:
        signals = {
            'current': retval['current'],
            'voltage': retval['voltage'],
            'current_range': retval['current_range'],
            'RX': retval['gpi[0]'],
            'TX': retval['gpi[1]'],
        }
    
        analysis = CurrentProfileAnalyzer(
            signals=signals,
            threshold_on=0.0005,
            threshold_off=0.0005,
            # voltage_override=3.3,
            min_thd_duration_start_us=5,
            min_thd_duration_end_us=250,
            min_window_duration_us=500,
        )
        analysis.process()
    
    """
    def __init__(self, signals, threshold_on, threshold_off,
                 voltage_override=None, min_thd_duration_start_us=0, min_thd_duration_end_us=0,
                 default_subwindow_label='EM0/1', min_window_duration_us=0, profile_name="current_profile"):
        self.units = {
            'current': 'A',
            'voltage': 'V',
            'duration': 's',
            'energy': 'J'
        }
        # Store raw signals
        self.raw_signals = {k: {'samples': np.copy(v['samples']), 'timestamp': np.copy(v['timestamp'])}
                            for k, v in signals.items()}
        self.signals = {k: {'samples': np.copy(v['samples']), 'timestamp': np.copy(v['timestamp'])}
                        for k, v in signals.items()}
        self.normalized_indices = {}
        self.threshold_on = threshold_on
        self.threshold_off = threshold_off
        self.voltage_override = voltage_override
        self.min_thd_duration_start_us = min_thd_duration_start_us
        self.min_thd_duration_end_us = min_thd_duration_end_us
        self.min_window_duration_us = min_window_duration_us
        self.default_subwindow_label = default_subwindow_label
        self.windows = []
        self.intervals = []
        self.window_stats = []
        self.sub_windows = []

        # Align current and voltage signals
        c_len = len(self.signals['current']['samples'])
        v_len = len(self.signals.get('voltage', {}).get('samples', []))
        if v_len and c_len != v_len:
            min_len = min(c_len, v_len)
            self.signals['current']['samples'] = self.signals['current']['samples'][:min_len]
            self.signals['current']['timestamp'] = self.signals['current']['timestamp'][:min_len]
            self.signals['voltage']['samples'] = self.signals['voltage']['samples'][:min_len]
            self.signals['voltage']['timestamp'] = self.signals['voltage']['timestamp'][:min_len]


        self.aggr_event_stats = None
        self.aggr_sub_event_stats = None
        self.aggr_interval_stats = None
        self.profile_name = profile_name
        pass

    def process(self, print_summary=False):
        self.find_windows()
        self.normalize_meas_duration()
        self.find_sub_windows()
        self.analyze_windows()
        self.analyze_sub_windows()
        self.analyze_intervals()
        if print_summary:
            self.print_summary()
        self.aggr_event_stats = self._aggregate_aggr_event_stats()
        self.aggr_sub_event_stats = self._aggregate_aggr_sub_event_stats()
        self.aggr_interval_stats = self._aggregate_interval_stats()

    def normalize_meas_duration(self):
        """
        Normalizing here refers to narrowing the aperture for analysis such that only full events and intervals are calculated
        Normalizing steps:
        1) Discard last window
        2) Get the indexes for narrowed aperture (start_idx, end_idx)
        3) Re-acquire intervals

        Returns:
            self.windows: list of windows after norm, each window is represented by tuple pair (start_idx, end_idx)
            self.intervals: list of intervals after norm, each interval is represented by tuple pair (start_idx, end_idx)

        """
        if not self.windows:
            return

        # Discard the last window
        start_idx = self.windows[0][0]
        end_idx = self.windows[-1][0] + 1
        # Discard the last window
        self.windows = self.windows[:-1]
        if not self.windows:
            return
        t0 = self.raw_signals['current']['timestamp'][start_idx]
        t1 = self.raw_signals['current']['timestamp'][end_idx - 1]

        # Reset indexes for all signals based on start_idx and end_idx
        for key, sig in self.raw_signals.items():
            ts = sig['timestamp']
            samples = sig['samples']
            mask = (ts >= t0) & (ts <= t1)
            self.signals[key]['samples'] = samples[mask]
            self.signals[key]['timestamp'] = ts[mask]
            # For current and voltage, store indices; for others, store time window
            if key in ['current', 'voltage']:
                self.normalized_indices[key] = (start_idx, end_idx)
            else:
                self.normalized_indices[key] = (t0, t1)
        offset = start_idx
        self.windows = [(s - offset, e - offset) for s, e in self.windows]
        self.intervals = []

        # Re-acquire intervals
        for i in range(1, len(self.windows)):
            prev_end = self.windows[i - 1][1]       # End of previous window
            curr_start = self.windows[i][0]         # Start of next window
            self.intervals.append((prev_end, curr_start))

        # Append last interval (no next window)
        prev_end = self.windows[-1][1]       # End of previous window
        self.intervals.append((prev_end, end_idx - start_idx - 1))

    def find_windows(self):
        """
        Identify events and intervals by "windowing" current profile according to current magnitude
        Returns:
            self.windows: list of windows, each window is represented by tuple pair (start_idx, end_idx)
            self.intervals: list of intervals, each interval is represented by tuple pair (start_idx, end_idx)

        """
        current = self.signals['current']['samples']
        timestamp = self.signals['current']['timestamp']
        sample_time = timestamp[1] - timestamp[0]
        min_samples_on = int(self.min_thd_duration_start_us * 1e-6 / sample_time)
        min_samples_off = int(self.min_thd_duration_end_us * 1e-6 / sample_time)
        n = len(current)
        idx = 0
        start_idx = None
        end_idx = None
        search_start = True
        self.windows = []
        while idx < n:
            if search_start:
                while idx < n and current[idx] < self.threshold_on:
                    idx += 1
                if idx >= n:
                    break
                tmp_search_idx = idx
                qualified = False
                while tmp_search_idx < n and current[tmp_search_idx] >= self.threshold_on:
                    tmp_search_idx += 1
                    if (tmp_search_idx - idx) > min_samples_on:
                        qualified = True
                        break
                if not qualified:
                    idx = tmp_search_idx
                    continue
                else:
                    start_idx = idx
                    search_start = False
            else:
                while idx < n and current[idx] > self.threshold_off:
                    idx += 1
                if idx >= n:
                    break
                tmp_search_idx = idx
                qualified = False
                while tmp_search_idx < n and current[tmp_search_idx] < self.threshold_on:
                    tmp_search_idx += 1
                    if (tmp_search_idx - idx) > min_samples_off:
                        qualified = True
                        break
                if not qualified:
                    idx = tmp_search_idx
                    continue
                else:
                    end_idx = idx
                    win_duration_us = (timestamp[end_idx] - timestamp[start_idx]) * 1e6
                    # Discard if window starts at idx=0 and duration is too short
                    if not (start_idx == 0 and win_duration_us < self.min_window_duration_us):
                        self.windows.append((start_idx, end_idx))
                    search_start = True
            idx += 1
        self.intervals = []
        for i in range(1, len(self.windows)):
            prev_end = self.windows[i - 1][1]
            curr_start = self.windows[i][0]
            self.intervals.append((prev_end, curr_start))

    def find_sub_windows(self):
        """
        Based on GPIO signals (other than 'current', 'voltage', 'current_range'), identify sub-event in events
        The sub-events have the same labels as the GPIO signal labels

        Returns:
            self.sub_windows: Dictionary of
        """
        self.sub_windows = []
        current_timestamp = self.signals['current']['timestamp']
        for win_idx, (start, end) in enumerate(self.windows):
            win_t0 = current_timestamp[start]
            win_t1 = current_timestamp[end]
            events = []
            for label, sig in self.signals.items():
                if label in ['current', 'voltage', 'current_range']:
                    continue
                sig_ts = sig['timestamp']
                sig_samples = sig['samples']
                mask = (sig_ts >= win_t0) & (sig_ts <= win_t1)
                if not np.any(mask):
                    continue
                arr_win = sig_samples[mask]
                ts_win = sig_ts[mask]
                transitions = np.diff(arr_win.astype(int))
                for i, t in enumerate(transitions):
                    if t == 1:
                        events.append((ts_win[i + 1], 'start', label))
                    elif t == -1:
                        events.append((ts_win[i + 1], 'end', label))
                if arr_win[0] == 1:
                    events.append((ts_win[0], 'start', label))
                if arr_win[-1] == 1:
                    events.append((ts_win[-1], 'end', label))
            events.sort()
            sub_win_list = []
            active_labels = set()
            last_t = win_t0
            for t, typ, label in events:
                if t > last_t:
                    if active_labels:
                        lbls = self.default_subwindow_label + ',' + ','.join(sorted(active_labels))
                    else:
                        lbls = self.default_subwindow_label
                    sub_win_list.append({'start': last_t, 'end': t, 'labels': lbls})
                if typ == 'start':
                    active_labels.add(label)
                elif typ == 'end':
                    active_labels.discard(label)
                last_t = t
            if last_t < win_t1:
                if active_labels:
                    lbls = self.default_subwindow_label + ',' + ','.join(sorted(active_labels))
                else:
                    lbls = self.default_subwindow_label
                sub_win_list.append({'start': last_t, 'end': win_t1, 'labels': lbls})
            self.sub_windows.append(sub_win_list)

    def analyze_windows(self):
        """Utility function to analyze windows"""
        current = self.signals['current']['samples']
        voltage = self.signals.get('voltage', {}).get('samples', None)
        timestamp = self.signals['current']['timestamp']
        for start, end in self.windows:
            t0 = timestamp[start]
            t1 = timestamp[end]
            duration = t1 - t0
            avg_current = np.mean(current[start:end + 1])
            if self.voltage_override is not None:
                avg_voltage = self.voltage_override
                inst_voltage = np.full(end - start + 1, self.voltage_override)
            elif voltage is not None:
                avg_voltage = np.mean(voltage[start:end + 1])
                inst_voltage = voltage[start:end + 1]
            else:
                avg_voltage = 0
                inst_voltage = np.zeros(end - start + 1)
            inst_current = current[start:end + 1]
            # Calculate timestamp resolution (assume uniform sampling)
            if end > start:
                timestamp_resolution = np.mean(np.diff(timestamp[start:end + 1]))
            else:
                timestamp_resolution = 0
            # Energy as sum of instantaneous current * voltage * timestamp resolution
            energy = np.sum(inst_current[1:] * inst_voltage[1:]) * timestamp_resolution # Discard sample 0
            self.window_stats.append({
                'start': t0,
                'end': t1,
                'duration': duration,
                'avg_current': avg_current,
                'avg_voltage': avg_voltage,
                'energy': energy
            })

    def analyze_sub_windows(self):
        """Utility function to analyze sub-windows"""
        current = self.signals['current']['samples']
        current_ts = self.signals['current']['timestamp']
        voltage = self.signals.get('voltage', {}).get('samples', None)
        voltage_ts = self.signals.get('voltage', {}).get('timestamp', None)
        self.sub_window_stats = []
        for win_idx, sub_win_list in enumerate(self.sub_windows):
            stats_list = []
            for sub_win in sub_win_list:
                s_t, e_t = sub_win['start'], sub_win['end']
                s_idx = np.searchsorted(current_ts, s_t, side='left')
                e_idx = np.searchsorted(current_ts, e_t, side='right') - 1
                if e_idx < s_idx:
                    continue
                inst_current = current[s_idx:e_idx + 1]
                ts_window = current_ts[s_idx:e_idx + 1]
                if self.voltage_override is not None:
                    inst_voltage = np.full_like(inst_current, self.voltage_override)
                    avg_voltage = self.voltage_override
                elif voltage is not None and voltage_ts is not None:
                    inst_voltage = np.interp(ts_window, voltage_ts, voltage)
                    avg_voltage = np.mean(inst_voltage)
                else:
                    inst_voltage = np.zeros_like(inst_current)
                    avg_voltage = 0
                avg_current = np.mean(inst_current)
                # Calculate timestamp resolution (assume uniform sampling)
                if len(ts_window) > 1:
                    timestamp_resolution = np.mean(np.diff(ts_window))
                else:
                    timestamp_resolution = 0
                energy = np.sum(inst_current[1:] * inst_voltage[1:]) * timestamp_resolution # Discard sample 0
                duration = e_t - s_t
                stats_list.append({
                    'start': s_t,
                    'end': e_t,
                    'duration': duration,
                    'avg_current': avg_current,
                    'avg_voltage': avg_voltage,
                    'energy': energy,
                    'labels': sub_win['labels']
                })
            self.sub_window_stats.append(stats_list)

    def get_global_avg_current(self):
        """Return the global average current over all samples."""
        current = self.signals['current']['samples']
        return np.mean(current)

    def get_avg_dynamic_current(self):
        """Return the global average current over all windows."""
        return self.aggr_event_stats['current']['mean']

    def get_avg_window_duration(self):
        """Return the global average duration over all windows."""
        return self.aggr_event_stats['duration']['mean']

    def get_avg_sleep_current(self):
        """Return the global average current over all intervals."""
        return self.aggr_interval_stats['current']['mean']

    def get_avg_sleep_duration(self):
        """Return the global average duration over all intervals."""
        return self.aggr_interval_stats['duration']['mean']

    def analyze_intervals(self):
        current = self.signals['current']['samples']
        timestamp = self.signals['current']['timestamp']
        voltage = self.signals.get('voltage', {}).get('samples', None)
        voltage_override = self.voltage_override
        voltage_ts = self.signals.get('voltage', {}).get('timestamp', None)
        self.interval_stats = []
        for prev_end, curr_start in self.intervals:
            t0 = timestamp[prev_end]
            t1 = timestamp[curr_start]
            duration = t1 - t0
            idx_start = prev_end + 1
            idx_end = curr_start
            inst_current = current[idx_start:idx_end]
            ts_window = timestamp[idx_start:idx_end]
            if voltage_override is not None:
                inst_voltage = np.full_like(inst_current, voltage_override)
                avg_voltage = voltage_override
            elif voltage is not None and voltage_ts is not None:
                inst_voltage = np.interp(ts_window, voltage_ts, voltage)
                avg_voltage = np.mean(inst_voltage)
            else:
                inst_voltage = np.zeros_like(inst_current)
                avg_voltage = 0
            avg_current = np.mean(inst_current)
            if len(ts_window) > 1:
                timestamp_resolution = np.mean(np.diff(ts_window))
            else:
                timestamp_resolution = 0
            energy = np.sum(inst_current[1:] * inst_voltage[1:]) * timestamp_resolution # Discard sample 0
            self.interval_stats.append({
                'start': t0,
                'end': t1,
                'duration': duration,
                'avg_current': avg_current,
                'avg_voltage': avg_voltage,
                'energy': energy
            })

    def print_summary(self):
        print(f'Number of windows: {len(self.windows)}')
        for i, stats in enumerate(self.window_stats):
            print(f'Window {i+1}:')
            print(f'  Start: {stats["start"]:.6f}s, End: {stats["end"]:.6f}s, Duration: {stats["duration"]*1e6:.2f}us')
            print(f'  Avg Current: {stats["avg_current"]:.6f}A, Avg Voltage: {stats["avg_voltage"]:.6f}V')
            print(f'  Energy: {stats["energy"]:.9f}J')
            if hasattr(self, 'sub_window_stats'):
                print(f'  Sub-windows:')
                for sub_stats in self.sub_window_stats[i]:
                    lbl = sub_stats['labels'] if sub_stats['labels'] else '[unlabelled]'
                    print(f'    Labels: {lbl}, Start: {sub_stats["start"]:.6f}s, End: {sub_stats["end"]:.6f}s, '
                          f'Duration: {sub_stats["duration"]*1e6:.2f}us, '
                          f'Avg Current: {sub_stats["avg_current"]:.6f}A, Energy: {sub_stats["energy"]:.9f}J')
        print('Intervals between windows:')
        for i, stats in enumerate(self.interval_stats):
            print(f'  Interval {i+1}: Duration: {stats["duration"]*1e6:.2f}us, Avg Current: {stats["avg_current"]:.9f}A')

        print(f'Global average current: {self.get_global_avg_current()}')


    def plot_windows(self, plot_current=True, plot_voltage=True, plot_current_range=False,
                     show_thresholds=True, highlight_sub_windows=True, blocking=True):
        t_raw = self.raw_signals['current']['timestamp']
        t_raw_us = t_raw * 1e6
        current_raw = self.raw_signals['current']['samples']
        idx_range = self.normalized_indices.get('current', (0, len(current_raw)))
        norm_start, norm_end = idx_range

        n_subplots = int(plot_current) + int(plot_voltage) + int(plot_current_range)
        fig, axs = plt.subplots(n_subplots, 1, figsize=(12, 4 * n_subplots), sharex=True)
        fig.canvas.manager.set_window_title(f'{self.profile_name}')
        if n_subplots == 1:
            axs = [axs]

        unique_labels = set()
        if highlight_sub_windows and hasattr(self, 'sub_windows'):
            for sub_win_list in self.sub_windows:
                for sub_win in sub_win_list:
                    lbl = sub_win['labels'] if sub_win['labels'] else 'Unlabelled'
                    unique_labels.add(lbl)
        unique_labels = sorted(unique_labels)
        cmap = plt.get_cmap('tab10')
        label_colors = {lbl: cmap(i % 10) for i, lbl in enumerate(unique_labels)}

        discard_color = 'grey'
        discard_alpha = 0.2
        discard_patch = mpatches.Patch(color=discard_color, alpha=discard_alpha, label='Discarded')

        idx = 0
        if plot_current:
            axs[idx].plot(t_raw_us, current_raw, label='Raw Current (A)', color='gray', alpha=0.5)
            # if norm_start > 0:
            #     axs[idx].axvspan(t_raw_us[0], t_raw_us[norm_start], color=discard_color, alpha=discard_alpha)
            # if norm_end < len(t_raw_us):
            #     axs[idx].axvspan(t_raw_us[norm_end-1], t_raw_us[-1], color=discard_color, alpha=discard_alpha)
            t_norm_us = t_raw_us[norm_start:norm_end]
            current_norm = current_raw[norm_start:norm_end]
            axs[idx].plot(t_norm_us, current_norm, label='Normalized Current (A)', color='blue')
            if highlight_sub_windows and hasattr(self, 'sub_windows'):
                for i, sub_win_list in enumerate(self.sub_windows):
                    for sub_win in sub_win_list:
                        lbl = sub_win['labels'] if sub_win['labels'] else 'Unlabelled'
                        color = label_colors[lbl]
                        axs[idx].axvspan(sub_win['start'] * 1e6, sub_win['end'] * 1e6, color=color, alpha=0.4, label=None)
            if show_thresholds:
                axs[idx].axhline(self.threshold_on, color='red', linestyle='--', label='Threshold On')
                axs[idx].axhline(self.threshold_off, color='blue', linestyle='--', label='Threshold Off')
            handles, labels = axs[idx].get_legend_handles_labels()
            # handles.append(discard_patch)
            # labels.append('Discarded')
            if highlight_sub_windows:
                for lbl in unique_labels:
                    patch = mpatches.Patch(color=label_colors[lbl], alpha=0.4, label=lbl)
                    handles.append(patch)
                    labels.append(lbl)
            axs[idx].legend(handles, labels)
            mean_uA = np.mean(current_norm) * 1e6
            axs[idx].set_ylabel('Current (A)')
            axs[idx].set_title(f'{self.profile_name}\nCurrent (A) v.s. Time (us), mean={mean_uA:0.1f}uA')
            idx += 1

        if plot_voltage:
            voltage_raw = self.raw_signals.get('voltage', {}).get('samples', None)
            voltage_ts_raw = self.raw_signals.get('voltage', {}).get('timestamp', None)
            voltage_ts_raw_us = voltage_ts_raw * 1e6 if voltage_ts_raw is not None else None
            idx_range_v = self.normalized_indices.get('voltage', (0, len(voltage_raw) if voltage_raw is not None else 0))
            norm_start_v, norm_end_v = idx_range_v
            if voltage_raw is not None and voltage_ts_raw is not None:
                axs[idx].plot(voltage_ts_raw_us, voltage_raw, label='Raw Voltage (V)', color='gray', alpha=0.5)
                # if norm_start_v > 0:
                #     axs[idx].axvspan(voltage_ts_raw_us[0], voltage_ts_raw_us[norm_start_v], color=discard_color, alpha=discard_alpha)
                # if norm_end_v < len(voltage_ts_raw_us):
                #     axs[idx].axvspan(voltage_ts_raw_us[norm_end_v-1], voltage_ts_raw_us[-1], color=discard_color, alpha=discard_alpha)
                voltage_norm = voltage_raw[norm_start_v:norm_end_v]
                voltage_ts_norm_us = voltage_ts_raw_us[norm_start_v:norm_end_v]
                axs[idx].plot(voltage_ts_norm_us, voltage_norm, label='Normalized Voltage (V)', color='orange')
                if highlight_sub_windows and hasattr(self, 'sub_windows'):
                    for i, sub_win_list in enumerate(self.sub_windows):
                        for sub_win in sub_win_list:
                            lbl = sub_win['labels'] if sub_win['labels'] else 'Unlabelled'
                            color = label_colors[lbl]
                            axs[idx].axvspan(sub_win['start'] * 1e6, sub_win['end'] * 1e6, color=color, alpha=0.4, label=None)
            handles, labels = axs[idx].get_legend_handles_labels()
            # handles.append(discard_patch)
            # labels.append('Discarded')
            if highlight_sub_windows:
                for lbl in unique_labels:
                    patch = mpatches.Patch(color=label_colors[lbl], alpha=0.4, label=lbl)
                    handles.append(patch)
                    labels.append(lbl)
            axs[idx].legend(handles, labels)
            axs[idx].set_ylabel('Voltage (V)')
            axs[idx].set_title(f'{self.profile_name}\nVoltage (V) v.s. Time (us)')
            idx += 1

        if plot_current_range:
            current_range_raw = self.raw_signals.get('current_range', {}).get('samples', None)
            current_range_ts_raw = self.raw_signals.get('current_range', {}).get('timestamp', None)
            current_range_ts_raw_us = current_range_ts_raw * 1e6 if current_range_ts_raw is not None else None
            idx_range_cr = self.normalized_indices.get('current_range', (0,
                                                                                       len(current_range_raw) if current_range_raw is not None else 0))
            # idx_range_cr is (t0, t1) for current_range
            if current_range_raw is not None and current_range_ts_raw is not None:
                t0, t1 = idx_range_cr
                mask = (current_range_ts_raw >= t0) & (current_range_ts_raw <= t1)
                current_range_norm = current_range_raw[mask]
                current_range_ts_norm_us = current_range_ts_raw_us[mask]
                axs[idx].plot(current_range_ts_raw_us, current_range_raw, label='Raw Current Range', color='gray',
                              alpha=0.5)
                axs[idx].plot(current_range_ts_norm_us, current_range_norm, label='Normalized Current Range',
                              color='purple')
                unique_ranges = sorted(set(current_range_norm))
                range_map = {0: '10A', 1: '2A', 2: '180mA', 3: '18mA', 4: '1.8mA', 5: '180uA', 6: '18uA', 7: 'Off'}
                axs[idx].set_yticks(unique_ranges)
                axs[idx].set_yticklabels([range_map.get(int(r), str(r)) for r in unique_ranges])
                if highlight_sub_windows and hasattr(self, 'sub_windows'):
                    for i, sub_win_list in enumerate(self.sub_windows):
                        for sub_win in sub_win_list:
                            lbl = sub_win['labels'] if sub_win['labels'] else 'Unlabelled'
                            color = label_colors[lbl]
                            axs[idx].axvspan(sub_win['start'] * 1e6, sub_win['end'] * 1e6, color=color, alpha=0.4,
                                             label=None)
            handles, labels = axs[idx].get_legend_handles_labels()
            if highlight_sub_windows:
                for lbl in unique_labels:
                    patch = mpatches.Patch(color=label_colors[lbl], alpha=0.4, label=lbl)
                    handles.append(patch)
                    labels.append(lbl)
            axs[idx].legend(handles, labels)
            axs[idx].set_ylabel('Current Range')
            axs[idx].set_title(f'{self.profile_name}\nCurrent Range v.s. Time (us)')

        axs[-1].set_xlabel('Time (us)')
        plt.tight_layout()
        if blocking:
            plt.show()

    def _compute_stats(self, arr):
        arr = np.array(arr)
        N = len(arr)
        mean = np.mean(arr) if N > 0 else 0
        std = np.std(arr) if N > 0 else 0
        return {'N': N, 'mean': mean, 'std': std}

    def _aggregate_aggr_event_stats(self):
        avg_current = []
        avg_voltage = []
        duration = []
        energy = []
        for stats in self.window_stats:
            avg_current.append(stats['avg_current'] * 1e3)  # mA
            avg_voltage.append(stats['avg_voltage'])         # V
            duration.append(stats['duration'] * 1e6)         # us
            energy.append(stats['energy'] * 1e9)             # nJ
        return {
            'current': self._compute_stats(avg_current),
            'voltage': self._compute_stats(avg_voltage),
            'duration': self._compute_stats(duration),
            'energy': self._compute_stats(energy)
        }

    def _aggregate_aggr_sub_event_stats(self):
        label_map = {}
        for win_stats in getattr(self, 'sub_window_stats', []):
            for sub_stats in win_stats:
                lbl = sub_stats['labels'] if sub_stats['labels'] else '[unlabelled]'
                if lbl not in label_map:
                    label_map[lbl] = {'current': [], 'voltage': [], 'duration': [], 'energy': []}
                label_map[lbl]['current'].append(sub_stats['avg_current'] * 1e3)  # mA
                label_map[lbl]['voltage'].append(sub_stats['avg_voltage'])         # V
                label_map[lbl]['duration'].append(sub_stats['duration'] * 1e6)     # us
                label_map[lbl]['energy'].append(sub_stats['energy'] * 1e9)         # nJ
        result = {}
        for lbl, vals in label_map.items():
            result[lbl] = {
                'current': self._compute_stats(vals['current']),
                'voltage': self._compute_stats(vals['voltage']),
                'duration': self._compute_stats(vals['duration']),
                'energy': self._compute_stats(vals['energy'])
            }
        return result

    def _aggregate_interval_stats(self):
        avg_current = []
        avg_voltage = []
        duration = []
        energy = []
        for stats in getattr(self, 'interval_stats', []):
            avg_current.append(stats['avg_current'] * 1e3)  # mA
            duration.append(stats['duration'] * 1e6)         # us
            if self.voltage_override is not None:
                avg_voltage.append(self.voltage_override)    # V
            else:
                avg_voltage.append(stats.get('avg_voltage', 0))
            energy.append(stats.get('energy', 0) * 1e9)      # nJ
        return {
            'current': self._compute_stats(avg_current),
            'voltage': self._compute_stats(avg_voltage),
            'duration': self._compute_stats(duration),
            'energy': self._compute_stats(energy)
        }

    def print_statistics(self):
        print("Aggregated Event Statistics:")
        print(f"  Current (mA): N={self.aggr_event_stats['current']['N']}, Mean={self.aggr_event_stats['current']['mean']:.6f}, Std={self.aggr_event_stats['current']['std']:.6f}")
        print(f"  Voltage (V): N={self.aggr_event_stats['voltage']['N']}, Mean={self.aggr_event_stats['voltage']['mean']:.6f}, Std={self.aggr_event_stats['voltage']['std']:.6f}")
        print(f"  Duration (us): N={self.aggr_event_stats['duration']['N']}, Mean={self.aggr_event_stats['duration']['mean']:.2f}, Std={self.aggr_event_stats['duration']['std']:.2f}")
        print(f"  Energy (nJ): N={self.aggr_event_stats['energy']['N']}, Mean={self.aggr_event_stats['energy']['mean']:.6f}, Std={self.aggr_event_stats['energy']['std']:.6f}")
        print("\nAggregated Sub-Event Statistics (by label):")
        for lbl, stats in self.aggr_sub_event_stats.items():
            print(f"  Label: {lbl}")
            print(f"    Current (mA): N={stats['current']['N']}, Mean={stats['current']['mean']:.6f}, Std={stats['current']['std']:.6f}")
            print(f"    Voltage (V): N={stats['voltage']['N']}, Mean={stats['voltage']['mean']:.6f}, Std={stats['voltage']['std']:.6f}")
            print(f"    Duration (us): N={stats['duration']['N']}, Mean={stats['duration']['mean']:.2f}, Std={stats['duration']['std']:.2f}")
            print(f"    Energy (nJ): N={stats['energy']['N']}, Mean={stats['energy']['mean']:.6f}, Std={stats['energy']['std']:.6f}")
        print("\nAggregated Interval Statistics:")
        print(f"  Current (uA): N={self.aggr_interval_stats['current']['N']}, Mean={self.aggr_interval_stats['current']['mean']:.6f}, Std={self.aggr_interval_stats['current']['std']:.6f}")
        print(f"  Voltage (V): N={self.aggr_interval_stats['voltage']['N']}, Mean={self.aggr_interval_stats['voltage']['mean']:.6f}, Std={self.aggr_interval_stats['voltage']['std']:.6f}")
        print(f"  Duration (us): N={self.aggr_interval_stats['duration']['N']}, Mean={self.aggr_interval_stats['duration']['mean']:.2f}, Std={self.aggr_interval_stats['duration']['std']:.2f}")
        print(f"  Energy (nJ): N={self.aggr_interval_stats['energy']['N']}, Mean={self.aggr_interval_stats['energy']['mean']:.6f}, Std={self.aggr_interval_stats['energy']['std']:.6f}")

    def get_subevent_list(self):
        return [key for key in self.aggr_sub_event_stats]

    def plot_statistics(self):
        measure_keys = ['current', 'voltage', 'duration', 'energy']
        measure_units = {'current': 'mA', 'voltage': 'V', 'duration': 'us', 'energy': 'nJ'}
        measure_data = {
            'current': [stats['avg_current'] * 1e3 for stats in self.window_stats],
            'voltage': [stats['avg_voltage'] for stats in self.window_stats],
            'duration': [stats['duration'] * 1e6 for stats in self.window_stats],
            'energy': [stats['energy'] * 1e9 for stats in self.window_stats]
        }
        fig, axs = plt.subplots(2, 2, figsize=(12, 8))
        for idx, key in enumerate(measure_keys):
            ax = axs[idx // 2, idx % 2]
            data = measure_data[key]
            unit = measure_units[key]
            N = len(data)
            mean = np.mean(data) if N > 0 else 0
            std = np.std(data) if N > 0 else 0
            ax.hist(data, bins='auto', alpha=0.7, color='skyblue', edgecolor='black')
            ax.set_title(f'Event {key.capitalize()} Histogram')
            ax.set_xlabel(f'{key.capitalize()} ({unit})')
            ax.set_ylabel('Count')
            ax.annotate(f'N: {N}\nMean: {mean:.4f}\nStd: {std:.4f}', xy=(0.95, 0.95), xycoords='axes fraction',
                        ha='right', va='top', fontsize=10,
                        bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='gray', alpha=0.5))
        plt.tight_layout()
        plt.show()

        # Interval stats
        interval_data = {
            'current': [stats['avg_current'] * 1e6 for stats in self.interval_stats],
            'voltage': [stats.get('avg_voltage', self.voltage_override) for stats in self.interval_stats],
            'duration': [stats['duration'] * 1e6 for stats in self.interval_stats],
            'energy': [stats.get('energy', 0) * 1e9 for stats in self.interval_stats]
        }
        fig, axs = plt.subplots(2, 2, figsize=(12, 8))
        interval_units = {'current': 'mA', 'voltage': 'V', 'duration': 'us', 'energy': 'nJ'}
        for idx, key in enumerate(measure_keys):
            ax = axs[idx // 2, idx % 2]
            data = interval_data[key]
            unit = interval_units[key]
            N = len(data)
            mean = np.mean(data) if N > 0 else 0
            std = np.std(data) if N > 0 else 0
            ax.hist(data, bins='auto', alpha=0.7, color='lightgreen', edgecolor='black')
            ax.set_title(f'Interval {key.capitalize()} Histogram')
            ax.set_xlabel(f'{key.capitalize()} ({unit})')
            ax.set_ylabel('Count')
            ax.annotate(f'N: {N}\nMean: {mean:.4f}\nStd: {std:.4f}', xy=(0.95, 0.95), xycoords='axes fraction',
                        ha='right', va='top', fontsize=10,
                        bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='gray', alpha=0.5))
        plt.tight_layout()
        plt.show()

        # Sub-event stats: one plot per label
        for lbl, stats_list in self.aggr_sub_event_stats.items():
            sub_data = {
                'current': [],
                'voltage': [],
                'duration': [],
                'energy': []
            }
            for win_stats in getattr(self, 'sub_window_stats', []):
                for sub_stats in win_stats:
                    if (sub_stats['labels'] if sub_stats['labels'] else '[unlabelled]') == lbl:
                        sub_data['current'].append(sub_stats['avg_current'] * 1e3)
                        sub_data['voltage'].append(sub_stats['avg_voltage'])
                        sub_data['duration'].append(sub_stats['duration'] * 1e6)
                        sub_data['energy'].append(sub_stats['energy'] * 1e9)
            fig, axs = plt.subplots(2, 2, figsize=(12, 8))
            for idx, key in enumerate(measure_keys):
                ax = axs[idx // 2, idx % 2]
                data = sub_data[key]
                unit = measure_units[key]
                N = len(data)
                mean = np.mean(data) if N > 0 else 0
                std = np.std(data) if N > 0 else 0
                ax.hist(data, bins='auto', alpha=0.7, color='orange', edgecolor='black')
                ax.set_title(f'Sub-Event "{lbl}" {key.capitalize()} Histogram')
                ax.set_xlabel(f'{key.capitalize()} ({unit})')
                ax.set_ylabel('Count')
                ax.annotate(f'N: {N}\nMean: {mean:.4f}\nStd: {std:.4f}', xy=(0.95, 0.95), xycoords='axes fraction',
                            ha='right', va='top', fontsize=10,
                            bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='gray', alpha=0.5))
            plt.tight_layout()
            plt.show()

    def plot_avg_current_profile(self):

        # Get all window indices
        window_indices = [(start, end) for start, end in self.windows]
        current = self.signals['current']['samples']
        timestamp = self.signals['current']['timestamp']

        # Find the maximum window length
        max_len = max(end - start + 1 for start, end in window_indices)
        profiles = []
        for start, end in window_indices:
            profile = current[start:end + 1]
            # Pad with average interval current if needed
            pad_len = max_len - len(profile)
            if pad_len > 0 and hasattr(self, 'interval_stats') and self.interval_stats:
                avg_interval_current = np.mean([stats['avg_current'] for stats in self.interval_stats])
                pad_values = np.full(pad_len, avg_interval_current)
                profile = np.concatenate([profile, pad_values])
            elif pad_len > 0:
                pad_values = np.full(pad_len, 0)
                profile = np.concatenate([profile, pad_values])
            profiles.append(profile)

        # Compute average profile
        avg_profile = np.mean(profiles, axis=0)

        # Use time axis from the first window (assuming uniform sampling)
        dt = timestamp[1] - timestamp[0]
        t_us = np.arange(len(avg_profile)) * dt * 1e6

        plt.figure(figsize=(10, 5))
        for i, profile in enumerate(profiles):
            plt.plot(t_us, profile, alpha=0.3, label=f'Window {i + 1}')
        plt.plot(t_us, avg_profile, color='red', linewidth=2, label='Average Profile')
        plt.xlabel('Time (\u03BCs)')
        plt.ylabel('Current (A)')
        plt.title(f'Average Current Profile (N={len(profiles)} windows, mean current: {np.mean(avg_profile) * 1e3:0.3f}mA)')
        plt.legend()
        plt.tight_layout()
        plt.show()

if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(name)s %(message)s', level=logging.INFO)
    js = Joulescope(duration='1s', frequency=1000000, signals='i,r,v,0,1', post_process='profile')
    retval = js.run()

    signals = {
        'current': retval['current'],
        'voltage': retval['voltage'],
        'current_range': retval['current_range'],
        'RX': retval['gpi[0]'],
        'TX': retval['gpi[1]'],
    }

    analysis = CurrentProfileAnalyzer(
        signals=signals,
        threshold_on=0.0005,
        threshold_off=0.0005,
        # voltage_override=3.3,
        min_thd_duration_start_us=5,
        min_thd_duration_end_us=250,
        min_window_duration_us=500,
    )
    analysis.process()
    analysis.plot_windows(plot_voltage=True, plot_current_range=True)
    analysis.plot_avg_current_profile()
    analysis.plot_statistics()