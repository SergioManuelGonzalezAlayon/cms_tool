import subprocess

import cms_bench
import pytest
from cms_protocol import BenchIdentity


_COMMANDER_PROBE_OUTPUT = """\
Kit Information:
=======================================
Kit Name        : EFR32xG22 Wireless Starter Kit
Kit Part Number : WSTK6021A Rev. B00
J-Link Serial   : 440365029
Debug Mode      : MCU
Debug Part      : EFR32MG22C224F512IM40-C
AEM Supported   : True
VCOM Supported  : True

Board List:
=======================================
Name            : Wireless Pro Kit Mainboard
Part Number     : BRD4002A Rev. A07
Serial Number   : 254802503

Name            : EFR32xG22 2.4 GHz 6 dBm Radio Board
Part Number     : BRD4182A Rev. B06
Serial Number   : 212300218
Target Device   : EFR32MG22C224F512IM40-C

DONE
"""


def test_commander_probe_parser_selects_radio_board_not_mainboard() -> None:
    identity = cms_bench._parse_commander_probe_identity(_COMMANDER_PROBE_OUTPUT)

    assert identity.radio_board_part_number == "BRD4182A"
    assert identity.radio_board_revision == "B06"
    assert identity.radio_board_serial_number == "212300218"
    assert identity.target_device == "EFR32MG22C224F512IM40-C"
    assert identity.canonical_target_device == "EFR32MG22C224F512IM40"
    assert identity.dut_jlink_serial == "440365029"
    assert identity.vcom_supported is True


def test_commander_probe_parser_accepts_unrevisioned_board_and_false_vcom() -> None:
    output = _COMMANDER_PROBE_OUTPUT.replace("BRD4182A Rev. B06", "BRD4182A").replace(
        "VCOM Supported  : True", "VCOM Supported  : False"
    )

    identity = cms_bench._parse_commander_probe_identity(output)

    assert identity.radio_board_part_number == "BRD4182A"
    assert identity.radio_board_revision == ""
    assert identity.vcom_supported is False


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("EFR32MG22C224F512IM40-C", "EFR32MG22C224F512IM40"),
        (" efr32mg24b220f1536im48-a ", "EFR32MG24B220F1536IM48"),
        ("EFR32MG22C224F512IM40", "EFR32MG22C224F512IM40"),
        ("custom-device-C", "CUSTOM-DEVICE-C"),
    ],
)
def test_commander_device_canonicalization_is_conservative(raw: str, canonical: str) -> None:
    assert cms_bench._canonicalize_commander_device(raw) == canonical


def test_commander_probe_parser_rejects_missing_radio_board() -> None:
    output = _COMMANDER_PROBE_OUTPUT.replace(
        "Target Device   : EFR32MG22C224F512IM40-C\n", ""
    )

    with pytest.raises(ValueError, match="no radio-board entry"):
        cms_bench._parse_commander_probe_identity(output)


def test_commander_probe_parser_rejects_ambiguous_radio_board_entries() -> None:
    output = _COMMANDER_PROBE_OUTPUT.replace(
        "\nDONE\n",
        """
Name            : Another Radio Board
Part Number     : BRD4183A Rev. A01
Serial Number   : 212300219
Target Device   : EFR32MG24B220F1536IM48

DONE
""",
    )

    with pytest.raises(ValueError, match="multiple radio-board entries"):
        cms_bench._parse_commander_probe_identity(output)


def test_validated_commander_probe_returns_identity_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = cms_bench.CommanderProbeEvidence(
        command=("commander", "adapter", "probe", "-s", "440365029"),
        returncode=0,
        stdout=_COMMANDER_PROBE_OUTPUT,
    )
    monkeypatch.setattr(cms_bench, "_run_commander_probe", lambda *_args, **_kwargs: evidence)

    result = cms_bench._probe_dut_identity("commander", "440365029")

    assert result.evidence is evidence
    assert result.identity.radio_board_part_number == "BRD4182A"


@pytest.mark.parametrize(
    ("evidence", "expected_message"),
    [
        (
            cms_bench.CommanderProbeEvidence(
                command=("commander", "adapter", "probe", "-s", "440365029"),
                returncode=None,
                error="[Errno 2] No such file or directory: commander",
            ),
            "could not run",
        ),
        (
            cms_bench.CommanderProbeEvidence(
                command=("commander", "adapter", "probe", "-s", "440365029"),
                returncode=1,
                stderr="No debug adapter found",
            ),
            "exit code 1",
        ),
        (
            cms_bench.CommanderProbeEvidence(
                command=("commander", "adapter", "probe", "-s", "440365029"),
                returncode=0,
                stdout=_COMMANDER_PROBE_OUTPUT.replace(
                    "Target Device   : EFR32MG22C224F512IM40-C\n", ""
                ),
            ),
            "invalid",
        ),
    ],
)
def test_validated_commander_probe_rejects_untrustworthy_results(
    monkeypatch: pytest.MonkeyPatch,
    evidence: cms_bench.CommanderProbeEvidence,
    expected_message: str,
) -> None:
    monkeypatch.setattr(cms_bench, "_run_commander_probe", lambda *_args, **_kwargs: evidence)

    with pytest.raises(cms_bench.CommanderProbeError, match=expected_message) as raised:
        cms_bench._probe_dut_identity("commander", "440365029")

    assert raised.value.evidence is evidence


def test_validated_commander_probe_rejects_jlink_serial_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = cms_bench.CommanderProbeEvidence(
        command=("commander", "adapter", "probe", "-s", "440365029"),
        returncode=0,
        stdout=_COMMANDER_PROBE_OUTPUT.replace("J-Link Serial   : 440365029", "J-Link Serial   : 440365030"),
    )
    monkeypatch.setattr(cms_bench, "_run_commander_probe", lambda *_args, **_kwargs: evidence)

    with pytest.raises(cms_bench.CommanderProbeError, match="does not match"):
        cms_bench._probe_dut_identity("commander", "440365029")


def test_health_check_uses_a_fresh_commander_probe_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = cms_bench.CMSBench(
        identity=BenchIdentity(bench_id="cms-bench-001"),
        resources=cms_bench.BenchResources(
            board="brd4182a",
            device="EFR32MG22C224F512IM40",
            dut_probe_sn="440365029",
            tester_probe_sn="440365030",
            dut_serial_port="/dev/ttyACM0",
            tester_serial_port="/dev/ttyACM1",
            commander_path="commander",
        ),
    )
    calls: list[tuple[str, str]] = []
    result = cms_bench.CommanderProbeResult(
        evidence=cms_bench.CommanderProbeEvidence(
            command=("commander", "adapter", "probe", "-s", "440365029"),
            returncode=0,
            stdout=_COMMANDER_PROBE_OUTPUT,
        ),
        identity=cms_bench._parse_commander_probe_identity(_COMMANDER_PROBE_OUTPUT),
    )

    def fake_probe(commander_path: str, dut_probe_sn: str):
        calls.append((commander_path, dut_probe_sn))
        return result

    monkeypatch.setattr(cms_bench, "_probe_dut_identity", fake_probe)

    first = bench.health_check()
    second = bench.health_check()

    assert calls == [("commander", "440365029"), ("commander", "440365029")]
    assert first.identity.board == "BRD4182A"
    assert first.identity.device == "EFR32MG22C224F512IM40-C"
    assert first.detected_identity is not None
    assert first.detected_identity.target_device == "EFR32MG22C224F512IM40-C"
    assert first.commander_probe is not None
    assert second.commander_probe is not None


def test_health_check_reports_failed_identity_without_configured_identity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = cms_bench.CMSBench(
        identity=BenchIdentity(
            bench_id="cms-bench-001",
            board="configured-board",
            device="CONFIGURED-DEVICE",
        ),
        resources=cms_bench.BenchResources(
            board="configured-board",
            device="CONFIGURED-DEVICE",
            dut_probe_sn="440365029",
            tester_probe_sn="440365030",
            dut_serial_port="/dev/ttyACM0",
            tester_serial_port="/dev/ttyACM1",
            commander_path="commander",
        ),
    )
    evidence = cms_bench.CommanderProbeEvidence(
        command=("commander", "adapter", "probe", "-s", "440365029"),
        returncode=1,
        stderr="No debug adapter found",
    )

    def fake_probe(*_args, **_kwargs):
        raise cms_bench.CommanderProbeError("Commander DUT probe failed with exit code 1", evidence)

    monkeypatch.setattr(cms_bench, "_probe_dut_identity", fake_probe)

    report = bench.health_check()
    identity_check = next(check for check in report.checks if check.name == "dut_identity")

    assert report.identity.board == ""
    assert report.identity.device == ""
    assert report.detected_identity is None
    assert report.commander_probe is not None
    assert report.commander_probe.returncode == 1
    assert identity_check.status.value == "fail"
    assert "could not establish" in identity_check.summary


def test_health_check_reports_connectivity_mappings_separately_from_dut_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = cms_bench.CMSBench(
        identity=BenchIdentity(bench_id="cms-bench-001"),
        resources=cms_bench.BenchResources(
            board="legacy-configured-board",
            device="LEGACY-CONFIGURED-DEVICE",
            dut_probe_sn="440365029",
            tester_probe_sn="440365030",
            dut_serial_port="/dev/ttyACM0",
            tester_serial_port="/dev/ttyACM1",
            joulescope_serial="JS220-0001",
            commander_path="commander",
        ),
    )
    result = cms_bench.CommanderProbeResult(
        evidence=cms_bench.CommanderProbeEvidence(
            command=("commander", "adapter", "probe", "-s", "440365029"),
            returncode=0,
            stdout=_COMMANDER_PROBE_OUTPUT,
        ),
        identity=cms_bench._parse_commander_probe_identity(_COMMANDER_PROBE_OUTPUT),
    )
    monkeypatch.setattr(cms_bench, "_probe_dut_identity", lambda *_args, **_kwargs: result)

    report = bench.health_check()
    checks = {check.name: check for check in report.checks}

    assert {"commander", "dut_probe", "tester_probe", "dut_serial_port", "tester_serial_port", "joulescope"} <= checks.keys()
    assert checks["dut_identity"].status.value == "pass"
    assert "board" not in checks
    assert "device" not in checks


def test_commander_probe_captures_raw_process_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="probe output", stderr="probe warning")

    monkeypatch.setattr(cms_bench.subprocess, "run", fake_run)

    evidence = cms_bench._run_commander_probe("commander", "440012345")

    assert evidence.command == ("commander", "adapter", "probe", "-s", "440012345")
    assert evidence.returncode == 0
    assert evidence.stdout == "probe output"
    assert evidence.stderr == "probe warning"
    assert evidence.error == ""
    assert captured["command"] == evidence.command
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": cms_bench.COMMANDER_PROBE_TIMEOUT_S,
        "check": False,
    }


def test_commander_probe_retains_nonzero_process_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cms_bench.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 2, stdout="partial output", stderr="probe failed"
        ),
    )

    evidence = cms_bench._run_commander_probe("commander", "440012345")

    assert evidence.returncode == 2
    assert evidence.stdout == "partial output"
    assert evidence.stderr == "probe failed"
    assert evidence.error == ""


@pytest.mark.parametrize(
    ("raised", "expected_stdout", "expected_stderr"),
    [
        (FileNotFoundError("commander"), "", ""),
        (
            subprocess.TimeoutExpired(
                ["commander", "adapter", "probe"],
                15,
                output=b"partial output",
                stderr=b"partial error",
            ),
            "partial output",
            "partial error",
        ),
    ],
)
def test_commander_probe_returns_execution_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    def fake_run(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(cms_bench.subprocess, "run", fake_run)

    evidence = cms_bench._run_commander_probe("commander", "440012345")

    assert evidence.returncode is None
    assert evidence.stdout == expected_stdout
    assert evidence.stderr == expected_stderr
    assert evidence.error


@pytest.mark.parametrize(
    ("commander_path", "dut_probe_sn", "timeout_s"),
    [
        ("", "440012345", 15.0),
        ("commander", "", 15.0),
        ("commander", "440012345", 0.0),
    ],
)
def test_commander_probe_rejects_invalid_inputs(
    commander_path: str,
    dut_probe_sn: str,
    timeout_s: float,
) -> None:
    with pytest.raises(ValueError):
        cms_bench._run_commander_probe(commander_path, dut_probe_sn, timeout_s=timeout_s)
