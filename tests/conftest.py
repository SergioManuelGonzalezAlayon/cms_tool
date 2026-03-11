import json
import hashlib
import subprocess
import shutil
import logging
from pathlib import Path
from dataclasses import fields
import pytest
from apps.app_manager import read_dev_config as _read_dev_config

log = logging.getLogger(__name__)

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--cfg",
        action="store",
        default=None,
        help="Config filename inside the test's config folder, e.g. non_conn_adv_200ms.json",
    )

def _test_config_dir(request: pytest.FixtureRequest) -> Path:
    """
    Map nodeid like 'tests/ble.py::test_beacon' -> 'tests/configs/ble/beacon'
    """
    file_part, test_name = request.node.nodeid.split("::", 1)
    test_module = Path(file_part).stem  # 'ble'
    feature = test_name.removeprefix("test_")  # 'beacon'
    return Path("tests") / "configs" / test_module / feature


def _test_config_path(request: pytest.FixtureRequest) -> Path:
    cfg_name = request.config.getoption("--cfg")
    cfg_dir = _test_config_dir(request)

    if cfg_name:
        return cfg_dir / cfg_name
    return cfg_dir / "default.json"

def dataclass_from_dict(cls, data: dict):
    valid = {f.name for f in fields(cls)}
    unknown = set(data.keys()) - valid
    if unknown:
        raise ValueError(f"Unknown fields for {cls.__name__}: {sorted(unknown)}")
    return cls(**data)

@pytest.fixture
def Config(request: pytest.FixtureRequest) -> dict:
    cfg_path = _test_config_path(request)

    if not cfg_path.exists():
        pytest.fail(
            f"Missing config for {request.node.nodeid}\n"
            f"Looked for: {cfg_path}\n"
            "Run the config generator first or pass --cfg <file.json> "
            f"(within {cfg_path.parent})."
        )

    return json.loads(cfg_path.read_text(encoding="utf-8-sig"))


@pytest.fixture(autouse=True)
def JoulescopeCaptureContext(request: pytest.FixtureRequest) -> None:
    if "JoulescopeDev" not in request.fixturenames:
        return

    js = request.getfixturevalue("JoulescopeDev")
    js.capture_test_name = request.node.name.removeprefix("test_")
    cfg_opt = request.config.getoption("--cfg")
    js.capture_config_name = Path(cfg_opt).name if cfg_opt else "default.json"

def _flash(image: Path, probe_sn: str):
    exe = shutil.which("commander")
    if exe is None:
        pytest.fail(
            "Required tool 'commander' not found on PATH. "
            "Ensure Simplicity Studio CLI tools are installed and available."
        )
    subprocess.run([exe, "flash", str(image), "--serialno", probe_sn], check=True, text=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_metadata(image: Path, *, slcp: Path, board: str, project_name: str, probe_sn: str) -> dict:
    return {
        "project_name": project_name,
        "slcp": str(slcp),
        "board": board,
        "probe_sn": probe_sn,
        "image": str(image),
        "sha256": _file_sha256(image),
        "size": image.stat().st_size,
    }


def _project_output_dir(build_root: Path, slcp: Path, *, project_name: str) -> Path:
    candidates = [
        build_root / slcp.stem,
        build_root / project_name.removeprefix("ncp_ble_"),
        build_root / project_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _find_prebuilt_image(build_root: Path, *, slcp: Path, project_name: str) -> Path:
    out_dir = _project_output_dir(build_root, slcp, project_name=project_name)
    candidates = list((out_dir / "cmake_gcc").rglob("*.s37")) + list((out_dir / "cmake_gcc").rglob("*.hex"))
    if not candidates:
        pytest.fail(
            "Missing prebuilt firmware image.\n"
            f"Expected build output under: {out_dir}\n"
            f"SLCP: {slcp}\n"
            "Run `just build` or `make build` before executing pytest."
        )
    return candidates[0]


def _dut_state_path(build_root: Path) -> Path:
    return build_root / "dut_last_flashed.json"


def _tester_ready_path(build_root: Path) -> Path:
    return build_root / "tester_ready.json"


def _read_provision_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_provision_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _same_image(current: dict, previous: dict | None) -> bool:
    if not previous:
        return False
    keys = ("project_name", "slcp", "board", "probe_sn", "sha256")
    return all(current.get(key) == previous.get(key) for key in keys)


def _short_hash(image_meta: dict | None) -> str:
    if not image_meta:
        return "missing"
    return str(image_meta.get("sha256", "missing"))[:12]


def _log_flash_decision(label: str, required: dict, previous: dict | None, should_flash: bool) -> None:
    if should_flash:
        if previous:
            log.info(
                "%s flash required: hash changed %s -> %s image=%s",
                label,
                _short_hash(previous),
                _short_hash(required),
                required["image"],
            )
        else:
            log.info(
                "%s flash required: no previous state image=%s hash=%s",
                label,
                required["image"],
                _short_hash(required),
            )
        return

    log.info(
        "%s flash skipped: image unchanged image=%s hash=%s",
        label,
        required["image"],
        _short_hash(required),
    )


@pytest.fixture(autouse=True)
def ProvisionFirmware(request: pytest.FixtureRequest) -> None:
    if "Config" not in request.fixturenames:
        return

    cfg_path = str(_test_config_path(request).resolve())

    config = request.getfixturevalue("Config")
    fw = config.get("firmware", {})
    required = (
        "dut_slcp",
        "tester_slcp",
        "board",
    )
    missing = [key for key in required if key not in fw]
    if missing:
        pytest.fail("Missing 'firmware' fields in test config: " + ", ".join(missing))

    dev_cfg = _read_dev_config(Path("dev_config.json"))
    build_root_str = fw.get("build_root") or dev_cfg.get("firmware", {}).get("build_root")
    if not build_root_str:
        pytest.fail(
            "Missing build root. Set dev_config.json firmware.build_root "
            "or provide firmware.build_root in test config."
        )
    build_root = Path(build_root_str)
    dut_probe_sn = fw.get("dut_probe_sn") or dev_cfg.get("dut", {}).get("probe_sn")
    tester_probe_sn = fw.get("tester_probe_sn") or dev_cfg.get("tester", {}).get("probe_sn")
    if not dut_probe_sn or not tester_probe_sn:
        pytest.fail(
            "Missing probe serials. Set dev_config.json dut.probe_sn and tester.probe_sn "
            "or provide firmware.dut_probe_sn/tester_probe_sn in test config."
        )

    dut_state_path = _dut_state_path(build_root)
    tester_state_path = _tester_ready_path(build_root)
    previous_dut_state = _read_provision_state(dut_state_path)
    previous_tester_state = _read_provision_state(tester_state_path)

    dut_slcp = Path(fw["dut_slcp"])
    tester_slcp = Path(fw["tester_slcp"])
    dut_image = _find_prebuilt_image(build_root, slcp=dut_slcp, project_name="ncp_ble_dut")
    tester_image = _find_prebuilt_image(build_root, slcp=tester_slcp, project_name="ncp_ble_tester")

    dut_required = _image_metadata(
        dut_image,
        slcp=dut_slcp,
        board=fw["board"],
        project_name="ncp_ble_dut",
        probe_sn=dut_probe_sn,
    )
    tester_required = _image_metadata(
        tester_image,
        slcp=tester_slcp,
        board=fw["board"],
        project_name="ncp_ble_tester",
        probe_sn=tester_probe_sn,
    )

    dut_matches = _same_image(dut_required, previous_dut_state.get("last_flashed"))
    tester_already_flashed = bool(previous_tester_state.get("last_flashed"))

    _log_flash_decision("DUT", dut_required, previous_dut_state.get("last_flashed"), not dut_matches)
    if tester_already_flashed:
        log.info(
            "Tester flash skipped: tester_ready.json present image=%s hash=%s",
            tester_required["image"],
            _short_hash(tester_required),
        )
    else:
        log.info(
            "Tester flash required: tester_ready.json missing image=%s hash=%s",
            tester_required["image"],
            _short_hash(tester_required),
        )

    if not dut_matches:
        _flash(dut_image, dut_probe_sn)
        log.info("DUT flashed successfully: %s", dut_image)
    if not tester_already_flashed:
        _flash(tester_image, tester_probe_sn)
        log.info("Tester flashed successfully: %s", tester_image)

    _write_provision_state(
        dut_state_path,
        {
            "cfg_path": cfg_path,
            "status": "reused" if dut_matches else "flashed",
            "required": dut_required,
            "last_flashed": dut_required,
        },
    )
    _write_provision_state(
        tester_state_path,
        {
            "cfg_path": cfg_path,
            "status": "reused" if tester_already_flashed else "flashed_once",
            "required": tester_required,
            "last_flashed": previous_tester_state.get("last_flashed", tester_required),
        },
    )
