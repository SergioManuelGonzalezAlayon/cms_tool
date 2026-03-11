import json
import hashlib
import subprocess
import shutil
import logging
import time
from pathlib import Path
from dataclasses import fields
import pytest

log = logging.getLogger(__name__)

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--cfg",
        action="store",
        default=None,
        help="Config filename inside the test's config folder, e.g. non_conn_adv_200ms.json",
    )
    parser.addoption(
        "--provision-fw",
        action="store_true",
        help="Build+flash DUT and tester before tests",
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

def _run(cmd, cwd=None):
    exe = shutil.which(cmd[0])
    if exe is None:
        pytest.fail(
            f"Required tool '{cmd[0]}' not found on PATH. "
            "Ensure Simplicity Studio CLI tools and CMake are installed and available."
        )

    resolved_cmd = [exe, *cmd[1:]]
    subprocess.run(resolved_cmd, cwd=cwd, check=True, text=True)


def _read_dev_config(path: Path = Path("dev_config.json")) -> dict:
    if not path.exists():
        pytest.fail(f"Missing {path}. Required for probe serial fallback.")
    return json.loads(path.read_text(encoding="utf-8-sig"))

def _upgrade_generated_slcp(slcp: Path, out_dir: Path) -> Path:
    generated_slcp = out_dir / slcp.name
    shutil.copy2(slcp, generated_slcp)
    _run(["slc", "upgrade", "-p", generated_slcp.name], cwd=out_dir)
    return generated_slcp


def _build_slcp(slcp: Path, out_dir: Path, project_name: str, board: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("FW build start: project=%s slcp=%s out=%s board=%s", project_name, slcp, out_dir, board)
    t0 = time.monotonic()

    _run(["slc", "generate", str(slcp), "-d", str(out_dir), f"-name={project_name}", "--with", board])
    log.info("FW generate done: project=%s", project_name)
    generated_slcp = _upgrade_generated_slcp(slcp, out_dir)
    log.info("FW upgrade done: project=%s slcp=%s", project_name, generated_slcp)
    _run(["cmake", "--workflow", "--preset", "project"], cwd=out_dir / "cmake_gcc")
    log.info("FW compile done: project=%s", project_name)

    candidates = list((out_dir / "cmake_gcc").rglob("*.s37")) + list((out_dir / "cmake_gcc").rglob("*.hex"))
    if not candidates:
        raise RuntimeError(f"No firmware image found under {out_dir}")
    log.info("FW build complete: project=%s image=%s elapsed=%.1fs", project_name, candidates[0], time.monotonic() - t0)
    return candidates[0]


def _flash(image: Path, probe_sn: str):
    _run(["commander", "flash", str(image), "--serialno", probe_sn])


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


def _provision_state_path(build_root: Path) -> Path:
    return build_root / "provision_state.json"


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


@pytest.fixture(autouse=True)
def ProvisionFirmware(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--provision-fw"):
        return

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

    dev_cfg = _read_dev_config()
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

    state_path = _provision_state_path(build_root)
    previous_state = _read_provision_state(state_path)

    dut_image = _build_slcp(
        slcp=Path(fw["dut_slcp"]),
        out_dir=build_root / "dut",
        project_name="ncp_ble_dut",
        board=fw["board"],
    )
    tester_image = _build_slcp(
        slcp=Path(fw["tester_slcp"]),
        out_dir=build_root / "tester",
        project_name="ncp_ble_tester",
        board=fw["board"],
    )

    required_state = {
        "cfg_path": cfg_path,
        "board": fw["board"],
        "dut": _image_metadata(
            dut_image,
            slcp=Path(fw["dut_slcp"]),
            board=fw["board"],
            project_name="ncp_ble_dut",
            probe_sn=dut_probe_sn,
        ),
        "tester": _image_metadata(
            tester_image,
            slcp=Path(fw["tester_slcp"]),
            board=fw["board"],
            project_name="ncp_ble_tester",
            probe_sn=tester_probe_sn,
        ),
    }

    last_flashed = previous_state.get("last_flashed", {})
    dut_matches = _same_image(required_state["dut"], last_flashed.get("dut"))
    tester_matches = _same_image(required_state["tester"], last_flashed.get("tester"))

    if not dut_matches:
        _flash(dut_image, dut_probe_sn)
    if not tester_matches:
        _flash(tester_image, tester_probe_sn)

    _write_provision_state(
        state_path,
        {
            "cfg_path": cfg_path,
            "status": "reused" if dut_matches and tester_matches else "flashed",
            "required": required_state,
            "last_flashed": {
                "cfg_path": cfg_path,
                "board": fw["board"],
                "dut": required_state["dut"],
                "tester": required_state["tester"],
            },
        },
    )
