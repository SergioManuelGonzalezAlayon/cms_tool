from pathlib import Path

import cms_bench


CONFIG_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "configs" / "cms_bench_runtime.json"


def test_reference_bench_runtime_config_loads_joulescope_model_and_mappings():
    identity, resources = cms_bench.load_bench_runtime_config(CONFIG_PATH)

    assert identity.bench_id == "cms-bench-dummy-001"
    assert identity.hostname == "cms-bench-dummy-host"
    assert identity.profile == "bench-lite"
    assert resources.joulescope_serial == "DUMMY-JOULESCOPE-USB-ID"
    assert resources.joulescope_model == "js320"
    assert resources.joulescope_wake_gpio == 4
    assert resources.dut_probe_sn == "DUMMY-DUT-PROBE"
    assert resources.tester_probe_sn == "DUMMY-TESTER-PROBE"
    assert resources.board == "BRD4182A"
    assert resources.device == "EFR32MG22C224F512IM40-C"
