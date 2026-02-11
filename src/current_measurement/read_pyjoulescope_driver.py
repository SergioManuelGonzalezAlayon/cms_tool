

# Usage example:
# js = Joulescope(signals='current,gpi[0]', duration=2.0, frequency=1000000, post_process='profile')
# retval = js.run()
# plt.plot(retval['current']['timestamp'], retval['current']['samples'])
# plt.show()

import logging
import matplotlib.pyplot as plt
from pyjoulescope_driver import Driver, time64
from pyjoulescope_driver.record import _SIGNALS, _signal_name_map
from queue import Queue, Empty
import numpy as np

SIGNAL_MAP = _signal_name_map()

# Note on driver publish/subscribe:
# pyjoulescope_driver uses an internal topic bus (like ROS topics, but local to this process).
# publish(...) sets device controls or enables streams, subscribe(...) registers callbacks for data topics.

class Joulescope:
    """
    The Joulescope class is a Python object for capturing and processing data from Joulescope devices.
    If post_process='average': then it returns a dictionary mapping signal names to their mean value
    and statistics. If post_process='profile' it returns a dictionary mapping signal names to their
    sample arrays, timestamps, and units.

    signals (str): Comma-separated list of signal names to capture (e.g., 'current,voltage,gpi[0]' or 'i,v,0').
    duration (float or str): Duration of capture in seconds or as a string (e.g., '1s').
    frequency (int, optional): Sampling frequency in Hz.
    serial_number (str, optional): Device serial number to select a specific Joulescope.
    open_mode (str): Device open mode (default: 'defaults').
    vref (str): GPIO reference voltage (default: '3v3').
    post_process (str): Post-processing method ('average' or 'profile').
    irange (str): Current range setting (default: 'auto').
    verbose (bool): Enable verbose output.
"""
    def __init__(self, signals='current,voltage',
                 duration=1.0,
                 frequency=None,
                 serial_number=None,
                 open_mode='defaults',
                 vref='3v3',
                 post_process='average',
                 irange='auto',
                 verbose=False):
        self.signals = [SIGNAL_MAP[s.lower()] for s in signals.split(',')]
        self.duration = time64.duration_to_seconds(duration) if isinstance(duration, str) else duration
        self.frequency = frequency
        self.serial_number = serial_number
        self.open_mode = open_mode
        self.vref = vref
        self.post_process = post_process
        self.verbose = verbose
        self.irange = irange
        self.retval = None
        self.driver = None
        self.capture_data = None

    def _verbose(self, msg):
        if self.verbose:
            print(msg)

    def create_driver(self):
        self.driver = Driver()
        return self.driver

    def open_devices(self):
        self.device_paths, self.signals_full = self._open_devices(self.driver)

    def close_driver(self):
        if self.driver is None:
            return

        # close devices first
        for p in getattr(self, "device_paths", []):
            try:
                self.driver.close(p)
            except Exception:
                pass

        self.driver.finalize()
        self.driver = None

    def _require_driver(self):
        if self.driver is None:
            raise RuntimeError('Driver not initialized')

    def _open_devices(self, jsdrv: Driver):
        jsdrv.log_level = 'WARNING'
        device_paths = sorted(jsdrv.device_paths())
        if self.serial_number is not None:
            serial_number_suffix = '/' + self.serial_number.lower()
            device_paths = [p for p in device_paths if p.lower().endswith(serial_number_suffix)]
        if len(device_paths) == 0:
            raise RuntimeError('Device not found')
        signals_full = []
        device_paths_success = []
        for device_path in device_paths:
            jsdrv.open(device_path, mode=self.open_mode)
            if self.open_mode == 'defaults':
                if 'js110' in device_path:
                    jsdrv.publish(f'{device_path}/s/v/range/select', '15 V')
                elif 'js220' in device_path:
                    jsdrv.publish(f'{device_path}/s/v/range/mode', 'auto')
                else:
                    print(f'Unsupported device {device_path}: ignore')
                    jsdrv.close(device_path)
                    continue

            # Set current range
            if self.irange == 'auto':
                if 'js110' in device_path:
                    jsdrv.publish(f'{device_path}/s/i/range/select', 'auto')
                elif 'js220' in device_path:
                    jsdrv.publish(f'{device_path}/s/i/range/mode', 'auto')
            else:
                if 'js110' in device_path:
                    jsdrv.publish(f'{device_path}/s/i/range/select', self.irange)
                elif 'js220' in device_path:
                    jsdrv.publish(f'{device_path}/s/i/range/mode', 'manual')
                    jsdrv.publish(f'{device_path}/s/i/range/select', self.irange)

            gpio_list = [0, 1, 2, 3]
            for gpio_num in gpio_list:
                if str(gpio_num) in [s.lower() for s in self.signals]:
                    jsdrv.publish(f'{device_path}/c/gpio/vref', self.vref)
                    jsdrv.publish(f'{device_path}/s/gpi/{gpio_num}/ctrl', 'on')
            if self.frequency is not None:
                jsdrv.publish(f'{device_path}/h/fs', int(self.frequency))
            signals_full.extend([(device_path, signal) for signal in self.signals])
            device_paths_success.append(device_path)
        return device_paths_success, signals_full

    def run(self):
        data_list = []
        with Driver() as jsdrv:
            device_paths, signals_full = self._open_devices(jsdrv)
            try:
                data = self.read(jsdrv, signals_full, duration=self.duration)
                data_list.append(data)
            finally:
                for device_path in device_paths:
                    jsdrv.close(device_path)
        match self.post_process:
            case 'average':
                self.retval = post_process_average(self, data_list[0])
            case 'profile':
                self.retval = get_profile(self, data_list[0])
        return self.retval

    def read(self, jsdrv: Driver, signals, duration: float, on_progress=None):
        """
        Read data from one or more Joulescopes.

        :param jsdrv: The pyjoulescope_driver.Driver instance.
        :param signals: The list of signals strings given as either
            oignal (presumes only one device connected) or
            tuple of (device_prefix, signal).
        :param duration: The capture duration in float seconds.
        :param on_progress: The optional callable(fract) to call with
            progress updates.
        :return: The mapping of (device_prefix, signal) to the data
            for each signal.  Each signal is also a map with keys:
            * device_path
            * signal
            * info
            * utc_range
            * sample_id_range
            * samples: The samples read from the device.
        """
        queue = Queue()
        read_state = {}
        if not callable(on_progress):
            on_progress = lambda x: None

        def signal_configure(signal_full):
            if isinstance(signal_full, str):
                device_path = jsdrv.device_paths()[0]
                signal_name = signal_full
            else:
                device_path, signal_name = signal_full
            info = _SIGNALS[signal_name]
            data_topic = f"{device_path}/{info['data_topic']}"
            ctrl_topic = f"{device_path}/{info['ctrl_topic']}"
            signal_state = {
                'device_path': device_path,
                'signal': signal_name,
                'info': info,
                'data_topic': data_topic,
                'ctrl_topic': ctrl_topic,
            }
            read_state[signal_full] = signal_state

            def data_fn(topic, value):
                # ['sample_id', 'utc', 'field_id', 'index', 'sample_rate', 'decimate_factor', 'time_map', 'data']
                # print(list(value.keys()))
                decimate_factor = value['decimate_factor']
                sample_id = value['sample_id'] // decimate_factor
                samples = value['data']
                is_digital = (info['signal_type'] == 'u1')  # 8 bits per u8 dtype
                if 'utc_range' not in signal_state:
                    signal_state['utc_range'] = [value['utc'], value['utc']]
                    signal_state['sample_id_range'] = [sample_id, sample_id]
                    signal_state['sample_rate'] = value['sample_rate'] // decimate_factor
                    sample_count = int((duration + 1.0) * signal_state['sample_rate'] + 1_000_000)  # over-allocate
                    signal_state['decimate_factor'] = decimate_factor
                    if is_digital:
                        sample_count //= 8
                    signal_state['samples'] = np.empty(sample_count, dtype=samples.dtype)
                sample_id_expect = signal_state['sample_id_range'][-1]
                if sample_id < sample_id_expect:
                    print('Unexpected repeat: unhandled')
                    return
                elif sample_id > sample_id_expect:
                    print(f'Skip {device_path}.{signal_name}: {sample_id} > {sample_id_expect}')
                    if samples.dtype in [np.float32, np.float64, float]:
                        signal_state['samples'][sample_id_expect:sample_id] = np.nan
                    elif is_digital:
                        signal_state['samples'][sample_id_expect // 8:sample_id // 8] = 0
                    else:
                        signal_state['samples'][sample_id_expect:sample_id] = 0
                sample_count = len(samples)
                sample_id_offset = sample_id - signal_state['sample_id_range'][0]
                if is_digital:
                    sample_id_offset //= 8
                    sample_count *= 8
                if sample_id_offset < len(signal_state['samples']):
                    e = sample_id_offset + len(samples)
                    if e > len(signal_state['samples']):
                        # should not happen since we overallocate the sample buffer
                        signal_state['samples'][sample_id_offset:e] = \
                            samples[:(len(signal_state['samples']) - sample_id_offset)]
                    else:
                        signal_state['samples'][sample_id_offset:e] = samples

                sample_id_next = sample_id + sample_count
                signal_state['sample_id_range'][-1] = sample_id_next
                signal_state['utc_range'][-1] = (value['utc'] +
                                                 int((sample_count / signal_state['sample_rate']) * time64.SECOND))
                queue.put(signal_full)

            def close(utc_range):
                jsdrv.unsubscribe(data_topic, data_fn, timeout=0)
                jsdrv.publish(ctrl_topic, 0)
                signal_state.pop('close', None)

                u0, u1 = utc_range
                a0, a1 = signal_state['utc_range']
                k0, k1 = signal_state['sample_id_range']
                n = k1 - k0
                samples = signal_state['samples']
                if signal_state['info']['signal_type'] == 'u1':
                    samples = np.unpackbits(samples, bitorder='little')
                s0 = int(n * ((u0 - a0) / (a1 - a0)))
                s1 = int(n * ((u1 - a0) / (a1 - a0)))
                z0 = int(((s0 - k0) / n) * (a1 - a0)) + a0
                z1 = int(((s1 - k0) / n) * (a1 - a0)) + a0
                sample_id_range = [k0 + s0, k0 + s1]
                signal_state['sample_id_range_decimated'] = sample_id_range
                signal_state['sample_id_range'] = [s * signal_state['decimate_factor'] for s in sample_id_range]
                signal_state['utc_range'] = [z0, z1]
                signal_state['samples'] = samples[s0:s1]

            signal_state['close'] = close
            jsdrv.subscribe(data_topic, ['pub'], data_fn)
            jsdrv.publish(ctrl_topic, 1, timeout=0)

        def time_info():
            utc_entries = [value.get('utc_range') for value in read_state.values()]
            if None in utc_entries:
                utc_start, utc_end = 0, 0
            else:
                utc_start = max([x[0] for x in utc_entries])
                utc_end = min([x[-1] for x in utc_entries])
            return {
                'duration': (utc_end - utc_start) / time64.SECOND,
                'utc_range': [utc_start, utc_end]
            }

        for signal in signals:
            signal_configure(signal)

        while True:
            try:
                queue.get(timeout=0.1)
            except Empty:
                continue
            duration_now = time_info()['duration']
            if duration_now >= duration:
                on_progress(1.0)
                break
            on_progress(duration_now / duration)

        u0, u1 = time_info()['utc_range']
        z1 = u0 + int(duration * time64.SECOND)
        utc_range = [u0, min(u1, z1)]
        for signal in read_state.values():
            signal['close'](utc_range)

        return read_state
    
    def capture(self, seconds: float, on_progress=None):
        """
        Capture for N seconds using the already-initialized driver and already-open devices.
        Returns the same structure as read(): read_state mapping.
        """
        if self.driver is None:
            raise RuntimeError("Driver not initialized")

        # if you stored this when you opened devices:
        if not getattr(self, "signals_full", None):
            raise RuntimeError("signals_full not set; did you open devices first?")

        self.capture_data = self.read(self.driver, self.signals_full, duration=float(seconds), on_progress=on_progress)


def verbose(args, msg):
    if args.verbose:
        print(msg)

def post_process_average(args, data):
    verbose(args, 'Display the mean (ignoring NaNs) for each signal')

    retval = dict()
    for key, value in data.items():
        samples = value['samples']
        samples_finite = np.isfinite(samples)
        sample_count = len(samples)
        nan_count = len(samples) - np.count_nonzero(samples_finite)
        nan_percent = nan_count / sample_count * 100

        if nan_count == 0:
            msg = f'{sample_count} samples'
        else:
            msg = f'ignored {nan_count} NaNs in {sample_count} samples => {nan_percent:.2f}%'
        v = np.mean(samples[samples_finite], dtype=np.float64)
        units = value['info']['units']
        print(f'{".".join(key)}: {v:24} {units} | {msg}')
        retval[key[1]] = {'value': v,
                       'sample_count': sample_count,
                       'nan_count': nan_count,
                       'nan_percent': nan_percent,
                       'units': units}
    return retval

def get_profile(args, data, is_verbose=False):
    if is_verbose:
        verbose(args, 'Returning the current profile for each channel')

    retval = dict()
    for key, value in data.items():
        samples = value['samples']
        if 'range' in key[1] or 'gpi' in key[1]:    # GPIO/range signals are always sampled at 2MHz
            sample_time_s = 1 / 2_000_000
        else:   # Current, voltages are always sampled at 2MHz but downsampled to maximum of 1MHz
            sample_time_s = 1 / args.frequency
        end_timestamp = len(samples) * sample_time_s
        timestamp = np.arange(0, end_timestamp, sample_time_s)[:len(samples)]
        units = value['info']['units']
        print(f'{".".join(key)}: {len(samples)} samples with units {units}')
        retval[key[1]] = {'samples': samples,
                       'timestamp': timestamp,
                       'units': units}
    return retval

if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(name)s %(message)s', level=logging.INFO)
    js = Joulescope(duration='1s', frequency=1000000, signals='i,r,0,1', post_process='profile')
    retval = js.run()

    plt.plot(retval['current']['timestamp'], retval['current']['samples'])
    plt.plot(retval['gpi[0]']['timestamp'], retval['gpi[0]']['samples'])
    plt.plot(retval['gpi[1]']['timestamp'], retval['gpi[1]']['samples'])
    plt.show()
    print("Done")
