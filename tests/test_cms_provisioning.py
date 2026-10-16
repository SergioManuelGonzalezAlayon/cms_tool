import json
from pathlib import Path
from types import SimpleNamespace

import conftest
from conftest import _firmware_project_name


def test_configured_dut_slcp_selects_the_matching_dcdc_or_no_dcdc_project() -> None:
    assert _firmware_project_name(Path("apps/ncp_ble_dut_dcdc.slcp")) == "ncp_ble_dut_dcdc"
    assert _firmware_project_name(Path("apps/ncp_ble_dut.slcp")) == "ncp_ble_dut"


def test_failed_startup_reuses_firmware_already_sent_for_same_measurement(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    state_root = tmp_path / "provision-state"
    build_root = tmp_path / "build"
    dut_slcp = tmp_path / "ncp_ble_dut.slcp"
    tester_slcp = tmp_path / "ncp_ble_tester.slcp"
    dut_image = build_root / dut_slcp.stem / "cmake_gcc" / "dut.s37"
    tester_image = build_root / tester_slcp.stem / "cmake_gcc" / "tester.s37"
    dut_image.parent.mkdir(parents=True)
    tester_image.parent.mkdir(parents=True)
    dut_image.write_bytes(b"dut image")
    tester_image.write_bytes(b"tester image")

    dev_config = {
        "dut": {"probe_sn": "dut-probe", "serial_port": "dut-port"},
        "tester": {"probe_sn": "tester-probe", "serial_port": "tester-port"},
        "firmware": {
            "build_root": str(build_root),
            "provision_state_root": str(state_root),
        },
    }
    test_config = {
        "firmware": {
            "board": "brd4182a",
            "dut_slcp": str(dut_slcp),
            "tester_slcp": str(tester_slcp),
            "dut_probe_sn": "dut-probe",
            "tester_probe_sn": "tester-probe",
        }
    }
    metadata_path = tmp_path / "cms_fw_metadata.json"
    metadata_path.write_text(
        json.dumps({"identity": {"measurement_id": "measurement-1"}}),
        encoding="utf-8",
    )
    request = SimpleNamespace(
        fixturenames=["Config"],
        getfixturevalue=lambda name: test_config,
        config=SimpleNamespace(
            getoption=lambda option: str(metadata_path)
            if option == "--cms-fw-metadata"
            else None
        ),
    )
    flash_calls = []

    monkeypatch.setattr(conftest, "_test_config_path", lambda request: tmp_path / "config.json")
    monkeypatch.setattr(conftest, "_effective_dev_config", lambda request: dev_config)
    monkeypatch.setattr(conftest, "_resolve_commander", lambda dev_cfg: "commander")
    monkeypatch.setattr(conftest, "_wait_for_serial_port", lambda serial_port: None)
    monkeypatch.setattr(
        conftest,
        "_flash",
        lambda image, probe_sn, commander=None: flash_calls.append((Path(image), probe_sn)),
    )
    caplog.set_level("INFO", logger=conftest.log.name)

    startup_results = iter([False, True])

    def run_attempt() -> bool:
        mark_device_ready = conftest.ProvisionFirmware.__wrapped__(request)
        # Provisioning runs before the BLE fixtures verify system.boot.
        startup_verified = next(startup_results)
        if startup_verified:
            mark_device_ready("dut")
            mark_device_ready("tester")
        return startup_verified

    assert run_attempt() is False
    assert f"Provisioning state root: {state_root}" in caplog.text
    assert f"DUT={state_root / 'dut_last_flashed.json'}" in caplog.text
    assert f"Tester={state_root / 'tester_ready.json'}" in caplog.text
    failed_dut_state = conftest._read_provision_state(state_root / "dut_last_flashed.json")
    failed_tester_state = conftest._read_provision_state(state_root / "tester_ready.json")
    assert failed_dut_state["status"] == "sent"
    assert failed_tester_state["status"] == "sent"
    assert failed_dut_state["measurement_id"] == "measurement-1"
    assert failed_tester_state["measurement_id"] == "measurement-1"
    assert failed_dut_state["readiness"] == "unverified"
    assert failed_tester_state["readiness"] == "unverified"
    assert "last_flashed" not in failed_dut_state
    assert "last_flashed" not in failed_tester_state
    assert failed_dut_state["last_sent"]["sha256"] == conftest._file_sha256(dut_image)
    assert failed_tester_state["last_sent"]["sha256"] == conftest._file_sha256(tester_image)
    assert run_attempt() is True
    verified_dut_state = conftest._read_provision_state(state_root / "dut_last_flashed.json")
    verified_tester_state = conftest._read_provision_state(state_root / "tester_ready.json")
    assert verified_dut_state["measurement_id"] == "measurement-1"
    assert verified_tester_state["measurement_id"] == "measurement-1"
    assert verified_dut_state["readiness"] == "verified"
    assert verified_tester_state["readiness"] == "verified"
    assert flash_calls == [
        (dut_image, "dut-probe"),
        (tester_image, "tester-probe"),
    ]


def test_sent_provision_state_reuse_requires_measurement_and_matching_firmware() -> None:
    required = {
        "project_name": "ncp_ble_dut",
        "slcp": "ncp_ble_dut.slcp",
        "board": "brd4182a",
        "probe_sn": "dut-probe",
        "sha256": "dut-hash",
    }
    state = {
        "measurement_id": "measurement-1",
        "status": "sent",
        "readiness": "unverified",
        "last_sent": required,
    }

    assert conftest._same_image(
        required,
        conftest._sent_last_flashed(state, "measurement-1"),
    )
    assert conftest._sent_last_flashed(state, "measurement-2") is None
    assert not conftest._same_image(
        {**required, "sha256": "other-hash"},
        conftest._sent_last_flashed(state, "measurement-1"),
    )


def test_provision_state_root_resolves_relative_paths_from_repository_root(tmp_path) -> None:
    relative_root = conftest._provision_state_root(
        tmp_path / "build",
        {"firmware": {"provision_state_root": "artifacts/state"}},
    )
    repository_root = Path(conftest.__file__).resolve().parent.parent

    assert relative_root == (repository_root / "artifacts/state").resolve()
    assert relative_root.is_absolute()

    absolute_root = tmp_path / "absolute-state"
    assert conftest._provision_state_root(
        tmp_path / "build",
        {"firmware": {"provision_state_root": str(absolute_root)}},
    ) == absolute_root.resolve()


def test_verified_provision_state_reuse_requires_measurement_and_matching_firmware() -> None:
    required = {
        "project_name": "ncp_ble_dut",
        "slcp": "ncp_ble_dut.slcp",
        "board": "brd4182a",
        "probe_sn": "dut-probe",
        "sha256": "dut-hash",
    }
    state = {
        "measurement_id": "measurement-1",
        "readiness": "verified",
        "last_flashed": required,
    }

    assert conftest._same_image(
        required,
        conftest._verified_last_flashed(state, "measurement-1"),
    )
    assert conftest._verified_last_flashed(state, "measurement-2") is None

    for field, value in (
        ("board", "brd4183"),
        ("probe_sn", "other-probe"),
        ("sha256", "other-hash"),
    ):
        changed = {**required, field: value}
        assert not conftest._same_image(
            changed,
            conftest._verified_last_flashed(state, "measurement-1"),
        )
