from cms_bench import (
    CMSBench,
    CMSBenchResources,
    CMSBenchResultAnalysis,
    CMSBenchState,
    resolve_cms_resources_from_dev_config,
    resolve_cms_resources_from_hwmux,
)


def test_resolve_cms_resources_from_dev_config_reads_expected_fields():
    dev_cfg = {
        "Joulescope": {"serial_number": "JS220-0001", "wake_gpio": 7},
        "dut": {"probe_sn": "440364944", "serial_port": "COM6"},
        "tester": {"probe_sn": "440365029", "serial_port": "COM8"},
        "firmware": {"board": "brd4182a", "device": "EFR32MG22"},
    }

    resources = resolve_cms_resources_from_dev_config(dev_cfg)

    assert resources == CMSBenchResources(
        dut_probe_sn="440364944",
        tester_probe_sn="440365029",
        dut_serial_port="COM6",
        tester_serial_port="COM8",
        joulescope_serial="JS220-0001",
        joulescope_wake_gpio=7,
        board="brd4182a",
        device="EFR32MG22",
        part_number="EFR32MG22",
    )


def test_resolve_cms_resources_from_hwmux_uses_role_metadata():
    group_json = {
        "id": 12,
        "name": "cms-bench-01",
        "metadata": {
            "cms_board": "brd4182a",
            "cms_device": "EFR32MG22C224F512IM40",
            "joulescope_dut_wake_gpio": 9,
        },
        "devices": [
            {
                "id": 101,
                "metadata": {
                    "Role": "DUT",
                    "jlink_serial_number": "440364944",
                    "serial_port": "COM6",
                },
                "part": {"board_no": "brd4182a", "variant": "EFR32MG22"},
            },
            {
                "id": 102,
                "metadata": {
                    "Role": "TESTER",
                    "jlink_serial_number": "440365029",
                    "serial_port": "COM8",
                },
                "part": {"board_no": "brd4182a", "variant": "EFR32MG22"},
            },
            {
                "id": 103,
                "metadata": {
                    "Role": "JOULESCOPE",
                    "serial_number": "JS220-0001",
                },
            },
        ],
    }

    resources = resolve_cms_resources_from_hwmux(group_json)

    assert resources.group_id == 12
    assert resources.group_name == "cms-bench-01"
    assert resources.dut_probe_sn == "440364944"
    assert resources.tester_probe_sn == "440365029"
    assert resources.dut_serial_port == "COM6"
    assert resources.tester_serial_port == "COM8"
    assert resources.joulescope_serial == "JS220-0001"
    assert resources.joulescope_wake_gpio == 9
    assert resources.board == "brd4182a"
    assert resources.device == "EFR32MG22C224F512IM40"
    assert resources.part_number == "EFR32MG22C224F512IM40"


def test_resolve_cms_resources_from_hwmux_requires_unique_roles():
    group_json = {
        "devices": [
            {"metadata": {"Role": "DUT", "jlink_serial_number": "1", "serial_port": "COM1"}},
            {"metadata": {"Role": "DUT", "jlink_serial_number": "2", "serial_port": "COM2"}},
            {"metadata": {"Role": "TESTER", "jlink_serial_number": "3", "serial_port": "COM3"}},
        ]
    }

    try:
        resolve_cms_resources_from_hwmux(group_json)
    except RuntimeError as exc:
        assert "multiple devices with Role=DUT" in str(exc)
    else:
        raise AssertionError("expected duplicated DUT role to fail")


def test_cms_bench_init_and_run_test_transitions_state():
    dev_cfg = {
        "dut": {"probe_sn": "440364944", "serial_port": "COM6"},
        "tester": {"probe_sn": "440365029", "serial_port": "COM8"},
    }
    bench = CMSBench()
    resources = bench.init(dev_config=dev_cfg, js="scope")

    assert bench.state is CMSBenchState.IDLE
    assert resources.joulescope_serial == "scope"
    assert bench.dut_serial == "440364944"
    assert bench.js_interface == "scope"

    observed: list[tuple[CMSBenchState, str | None]] = []

    def runner(request):
        observed.append((bench.state, request.dut_app))
        return request.analysis

    result = bench.run_test(
        dut_app="dut.s37",
        tester_app="tester.s37",
        analysis=CMSBenchResultAnalysis.DIFF,
        runner=runner,
    )

    assert result is CMSBenchResultAnalysis.DIFF
    assert observed == [(CMSBenchState.TESTING, "dut.s37")]
    assert bench.state is CMSBenchState.IDLE
