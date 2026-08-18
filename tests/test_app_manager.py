import subprocess
import json
import shutil
from pathlib import Path

import pytest

from apps import app_manager
from utils import config_loader


def test_run_accepts_noop_slc_upgrade_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_manager, "resolve_tool", lambda _: "slc")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            19,
            stdout=(
                "Nothing for Project Configuration Tools to upgrade.\n"
                "Nothing to upgrade.\n"
                "An upgrade was not performed.\n"
            ),
            stderr="",
        ),
    )

    result = app_manager.run(
        ["slc", "upgrade", "-p", str(Path("demo.slcp"))],
        ok_result=app_manager._is_noop_upgrade_result,
    )

    assert result.returncode == 19


def test_run_still_raises_for_other_nonzero_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_manager, "resolve_tool", lambda _: "slc")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            19,
            stdout="Upgrade failed for an unknown reason.\n",
            stderr="error\n",
        ),
    )

    with pytest.raises(subprocess.CalledProcessError):
        app_manager.run(
            ["slc", "upgrade", "-p", str(Path("demo.slcp"))],
            ok_result=app_manager._is_noop_upgrade_result,
        )


def test_validate_firmware_config_requires_board_device_and_sisdk() -> None:
    with pytest.raises(RuntimeError, match="target.board, target.device, target.sisdk"):
        app_manager.validate_firmware_config({})


def test_validate_firmware_config_requires_board_from_effective_config() -> None:
    with pytest.raises(RuntimeError, match="target.board"):
        app_manager.validate_firmware_config(
            {"firmware": {"device": "EFR32BG22C224F512GM40", "sisdk": "2025.12.2"}}
        )


def test_validate_firmware_config_uses_effective_config_values() -> None:
    result = app_manager.validate_firmware_config(
        {"firmware": {"board": "BRD4182A_B06", "device": "EFR32BG22C224F512GM40", "sisdk": "2025.12.2"}}
    )

    assert result == {
        "board": "BRD4182A_B06",
        "brev": "",
        "device": "EFR32BG22C224F512GM40",
        "sisdk": "2025.12.2",
    }


def test_list_installed_sisdks_only_returns_installed_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_manager,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=(
                "Packages content:\n"
                "  Id: simplicity-sdk\n"
                "  Version: 2025.6.2\n"
                "  Installer type: conan\n"
                "  Package type: sdk\n"
                "  Installation:\n"
                "    C:\\sdk\\2025.6.2\n"
                "\n"
                "  Id: simplicity-sdk\n"
                "  Version: 2025.12.2\n"
                "  Installer type: conan\n"
                "  Package type: sdk\n"
                "  Installation:\n"
                "    C:\\sdk\\2025.12.2\n"
                "\n"
                "  Id: simplicity-sdk\n"
                "  Version: 2025.12.3\n"
                "  Installer type: conan\n"
                "  Package type: sdk\n"
            ),
            stderr="",
        ),
    )

    result = app_manager.list_installed_sisdks()

    assert result == [
        {"id": "simplicity-sdk", "version": "2025.6.2", "path": "C:\\sdk\\2025.6.2"},
        {"id": "simplicity-sdk", "version": "2025.12.2", "path": "C:\\sdk\\2025.12.2"},
    ]


def test_effective_build_config_cli_overrides_client_config() -> None:
    overrides = app_manager.parse_args([
        "build",
        "--sdk-package-path", r"C:\sdk\cli",
        "--sisdk-version", "2026.1.0",
        "--build-root", "cli-build",
        "--slc", "cli-slc",
    ])

    result = app_manager.effective_build_config(
        {
            "target": {
                "board": "brd4182a",
                "board_rev": "a06",
                "device": "EFR32MG22C224F512IM40",
                "sisdk": "2025.12.3",
                "sisdk_package_path": r"C:\sdk\config",
            },
            "build": {"root": "config-build", "bootloader_slcp": "apps/boot.slcp"},
            "tools": {"slc": "config-slc"},
        },
        overrides,
    )

    assert result["firmware"] == {
        "board": "brd4182a",
        "brev": "a06",
        "device": "EFR32MG22C224F512IM40",
        "sisdk": "2026.1.0",
        "sisdk_package_path": r"C:\sdk\cli",
        "build_root": "cli-build",
        "bootloader_slcp": "apps/boot.slcp",
    }
    assert result["tools"]["slc"] == "cli-slc"


def test_resolve_build_root_uses_cli_before_client_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_manager,
        "read_client_config",
        lambda: {"build": {"root": "configured-build"}},
    )

    assert app_manager.resolve_build_root("cli-build") == Path("cli-build")
    assert app_manager.resolve_build_root(None) == Path("configured-build")


def test_resolve_bootloader_slcp_accepts_raw_client_config(tmp_path: Path) -> None:
    bootloader = tmp_path / "custom-bootloader.slcp"
    bootloader.write_text("project", encoding="utf-8")

    result = app_manager.resolve_bootloader_slcp(
        {"build": {"bootloader_slcp": str(bootloader)}}
    )

    assert result == bootloader


def test_resolve_sdk_selection_prefers_cli_then_config_then_slt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make_sdk(name: str) -> Path:
        sdk = tmp_path / name
        xapi = sdk / app_manager.SL_BT_XAPI_PATH
        xapi.parent.mkdir(parents=True)
        xapi.write_bytes(name.encode())
        return sdk

    cli_sdk = make_sdk("cli")
    config_sdk = make_sdk("config")
    slt_sdk = make_sdk("slt")
    monkeypatch.setattr(app_manager, "_sdk_git_commit", lambda path: "sdk-commit")
    monkeypatch.setattr(app_manager, "resolve_sisdk_path", lambda version: slt_sdk)

    cli = app_manager.resolve_sdk_selection(
        version="2026.1.0", cli_package_path=str(cli_sdk), config_package_path=str(config_sdk)
    )
    configured = app_manager.resolve_sdk_selection(
        version="2026.1.0", config_package_path=str(config_sdk)
    )
    slt = app_manager.resolve_sdk_selection(version="2026.1.0")

    assert (cli.path, cli.source) == (cli_sdk.resolve(), "cli_package_path")
    assert (configured.path, configured.source) == (config_sdk.resolve(), "client_config_package_path")
    assert (slt.path, slt.source) == (slt_sdk.resolve(), "slt")
    assert cli.commit == cli.sdk_hash == "sdk-commit"
    assert cli.xapi_hash == app_manager._sha256(cli_sdk / app_manager.SL_BT_XAPI_PATH)


def test_resolve_sdk_selection_reports_missing_and_ambiguous_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        app_manager.resolve_sdk_selection(
            version="2026.1.0", cli_package_path=str(tmp_path / "missing")
        )

    monkeypatch.setattr(
        app_manager,
        "list_installed_sisdks",
        lambda: [
            {"version": "2026.1.0", "path": "first"},
            {"version": "2026.1.0", "path": "second"},
        ],
    )
    with pytest.raises(RuntimeError, match="multiple installed paths"):
        app_manager.resolve_sdk_selection(version="2026.1.0")


def test_generate_slcp_passes_explicit_sdk_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    out_dir = Path("build/demo")

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(app_manager, "run", fake_run)

    app_manager.generate_slcp(
        Path("apps/demo.slcp"),
        out_dir=out_dir,
        board="brd4182a_b06",
        sdk_path=Path(r"C:\sdk\2025.12.2"),
    )

    assert captured == [[
        "slc",
        "generate",
        "apps\\demo.slcp",
        "--sdk-package-path",
        r"C:\sdk\2025.12.2",
        "-d",
        str(out_dir),
        "-name=demo",
        "--with",
        "brd4182a_b06",
        "--generator-timeout=60000",
    ]]


def test_copy_bgapi_xapi_stages_file_from_exact_sdk(tmp_path: Path) -> None:
    sdk_path = tmp_path / "selected-sdk"
    source_xapi = sdk_path / app_manager.SL_BT_XAPI_PATH
    source_xapi.parent.mkdir(parents=True)
    source_xapi.write_bytes(b"selected-sdk-xapi")
    build_root = tmp_path / "firmware-build"

    staged_xapi = app_manager.copy_bgapi_xapi(build_root, sdk_path)

    assert staged_xapi == build_root / "runtime" / "sl_bt.xapi"
    assert staged_xapi.read_bytes() == source_xapi.read_bytes()


def test_trust_sisdk_calls_slc_signature_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(app_manager, "run", fake_run)

    app_manager.trust_sisdk(Path(r"C:\sdk\2025.12.2"))

    assert captured == [["slc", "signature", "trust", "-s", r"C:\sdk\2025.12.2"]]


def test_upgrade_slcp_uses_generated_project_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    out_dir = Path("build/demo")
    generated = out_dir / "demo.slcp"

    monkeypatch.setattr(Path, "exists", lambda self: self == generated)

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(app_manager, "run", fake_run)

    result = app_manager.upgrade_slcp(Path("apps/demo.slcp"), out_dir=out_dir)

    assert result == generated
    assert captured == [["slc", "upgrade", "-p", str(generated.resolve())]]


def test_process_slcp_build_generates_then_builds_when_no_existing_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    slcp = Path("apps/demo.slcp")
    sdk_path = Path(r"C:\sdk\2025.12.2")
    build_root = Path("build")
    firmware_cfg = {
        "board": "brd4182a_b06",
        "brev": "b06",
        "device": "EFR32BG22C224F512GM40",
        "sisdk": "2025.12.2",
    }

    def fake_generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))

    def fake_upgrade(*args, **kwargs):
        calls.append(("upgrade", args, kwargs))
        return build_root / "demo.slcp"

    def fake_build(*args, **kwargs):
        calls.append(("build", args, kwargs))
        return build_root / "cmake_gcc" / "demo.s37"

    def fake_write_metadata(**kwargs):
        calls.append(("metadata", (), kwargs))
        return build_root / slcp.stem / app_manager.BUILD_METADATA_FILENAME

    monkeypatch.setattr(app_manager, "generate_slcp", fake_generate)
    monkeypatch.setattr(app_manager, "find_built_image", lambda out_dir: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(app_manager, "build_slcp", fake_build)
    monkeypatch.setattr(app_manager, "write_build_metadata", fake_write_metadata)

    result = app_manager.process_slcp(
        slcp,
        build_root=build_root,
        firmware_cfg=firmware_cfg,
        sdk_path=sdk_path,
        runtime_xapi_path=build_root / "runtime" / "sl_bt.xapi",
        action="build",
    )

    assert [entry[0] for entry in calls] == ["generate", "build", "metadata"]
    assert calls[0][2]["sdk_path"] == sdk_path
    assert result["generated_slcp"] == str(build_root / slcp.stem / slcp.name)
    assert result["image"] == str(build_root / "cmake_gcc" / "demo.s37")
    assert result["metadata"] == str(build_root / slcp.stem / app_manager.BUILD_METADATA_FILENAME)
    assert result["sdk_path"] == str(sdk_path)


def test_process_slcp_build_uses_existing_image_without_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    slcp = Path("apps/demo.slcp")
    sdk_path = Path(r"C:\sdk\2025.12.2")
    build_root = Path("build")
    existing_image = build_root / slcp.stem / "cmake_gcc" / "demo.s37"
    firmware_cfg = {
        "board": "brd4182a_b06",
        "brev": "b06",
        "device": "EFR32BG22C224F512GM40",
        "sisdk": "2025.12.2",
    }

    def fake_generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))

    def fake_build(*args, **kwargs):
        calls.append(("build", args, kwargs))
        return existing_image

    def fake_write_metadata(**kwargs):
        calls.append(("metadata", (), kwargs))
        return build_root / slcp.stem / app_manager.BUILD_METADATA_FILENAME

    monkeypatch.setattr(app_manager, "generate_slcp", fake_generate)
    monkeypatch.setattr(app_manager, "find_built_image", lambda out_dir: existing_image)
    monkeypatch.setattr(app_manager, "build_slcp", fake_build)
    monkeypatch.setattr(app_manager, "write_build_metadata", fake_write_metadata)

    result = app_manager.process_slcp(
        slcp,
        build_root=build_root,
        firmware_cfg=firmware_cfg,
        sdk_path=sdk_path,
        runtime_xapi_path=build_root / "runtime" / "sl_bt.xapi",
        action="build",
    )

    assert [entry[0] for entry in calls] == ["metadata"]
    assert result["image"] == str(existing_image)


def test_process_slcp_build_uses_generated_project_before_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    slcp = Path("apps/demo.slcp")
    sdk_path = Path(r"C:\sdk\2025.12.2")
    build_root = Path("build")
    generated_slcp = build_root / slcp.stem / slcp.name
    firmware_cfg = {
        "board": "brd4182a_b06",
        "brev": "b06",
        "device": "EFR32BG22C224F512GM40",
        "sisdk": "2025.12.2",
    }

    def fake_generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))

    def fake_build(*args, **kwargs):
        calls.append(("build", args, kwargs))
        return build_root / "cmake_gcc" / "demo.s37"

    def fake_write_metadata(**kwargs):
        calls.append(("metadata", (), kwargs))
        return build_root / slcp.stem / app_manager.BUILD_METADATA_FILENAME

    monkeypatch.setattr(app_manager, "generate_slcp", fake_generate)
    monkeypatch.setattr(app_manager, "find_built_image", lambda out_dir: None)
    monkeypatch.setattr(Path, "exists", lambda self: self == generated_slcp)
    monkeypatch.setattr(app_manager, "build_slcp", fake_build)
    monkeypatch.setattr(app_manager, "write_build_metadata", fake_write_metadata)

    result = app_manager.process_slcp(
        slcp,
        build_root=build_root,
        firmware_cfg=firmware_cfg,
        sdk_path=sdk_path,
        runtime_xapi_path=build_root / "runtime" / "sl_bt.xapi",
        action="build",
    )

    assert [entry[0] for entry in calls] == ["build", "metadata"]
    assert result["image"] == str(build_root / "cmake_gcc" / "demo.s37")


def test_process_slcp_upgrade_keeps_explicit_upgrade_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    slcp = Path("apps/demo.slcp")
    sdk_path = Path(r"C:\sdk\2025.12.2")
    build_root = Path("build")
    firmware_cfg = {
        "board": "brd4182a_b06",
        "brev": "b06",
        "device": "EFR32BG22C224F512GM40",
        "sisdk": "2025.12.2",
    }

    def fake_generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))

    def fake_upgrade(*args, **kwargs):
        calls.append(("upgrade", args, kwargs))
        return build_root / "demo.slcp"

    monkeypatch.setattr(app_manager, "generate_slcp", fake_generate)
    monkeypatch.setattr(app_manager, "upgrade_slcp", fake_upgrade)

    result = app_manager.process_slcp(
        slcp,
        build_root=build_root,
        firmware_cfg=firmware_cfg,
        sdk_path=sdk_path,
        runtime_xapi_path=build_root / "runtime" / "sl_bt.xapi",
        action="upgrade",
    )

    assert [entry[0] for entry in calls] == ["generate", "upgrade"]
    assert result["generated_slcp"] == str(build_root / "demo.slcp")
    assert result["image"] == ""


def test_write_build_metadata_records_firmware_and_artifacts() -> None:
    out_dir = Path("build/test_write_build_metadata/demo")
    if out_dir.parent.exists():
        shutil.rmtree(out_dir.parent)
    artifact_dir = out_dir / "cmake_gcc" / "build" / "base"
    artifact_dir.mkdir(parents=True)
    image = artifact_dir / "demo.s37"
    hex_image = artifact_dir / "demo.hex"
    map_file = artifact_dir / "demo.map"
    image.write_bytes(b"s37")
    hex_image.write_bytes(b"hex")
    map_file.write_bytes(b"map")
    sdk_path = Path(r"C:\sdk\2026.0.0")
    sdk_selection = app_manager.SDKSelection(
        path=sdk_path,
        version="2026.0.0",
        source="client_config_package_path",
        commit="sdk-commit",
        sdk_hash="sdk-hash",
        xapi_hash="xapi-hash",
    )

    metadata_path = app_manager.write_build_metadata(
        slcp=Path("apps/demo.slcp"),
        out_dir=out_dir,
        image=image,
        firmware_cfg={
            "board": "brd4182c",
            "brev": "a06",
            "device": "EFR32MG22C224F512IM40",
            "sisdk": "2026.0.0",
        },
        sdk_path=sdk_path,
        runtime_xapi_path=None,
        sdk_selection=sdk_selection,
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["project_name"] == "demo"
    assert metadata["selected_image"]["name"] == "demo.s37"
    assert metadata["firmware"] == {
        "board": "brd4182c",
        "brev": "a06",
        "device": "EFR32MG22C224F512IM40",
        "sisdk": "2026.0.0",
        "sdk_path": str(sdk_path.resolve()),
    }
    assert metadata["sdk"] == {
        "source": "client_config_package_path",
        "version": "2026.0.0",
        "package_path": str(sdk_path.resolve()),
        "commit": "sdk-commit",
        "hash": "sdk-hash",
        "xapi_sha256": "xapi-hash",
    }
    assert {artifact["name"] for artifact in metadata["artifacts"]} == {"demo.s37", "demo.hex", "demo.map"}


def test_write_fw_data_records_firmware_identity_and_role_hashes() -> None:
    build_root = Path("build/test_write_fw_data")
    if build_root.exists():
        shutil.rmtree(build_root)
    bootloader_out_dir = build_root / "bootloader-uart-bgapi"
    bootloader_dir = bootloader_out_dir / "cmake_gcc" / "build" / "base"
    bootloader_dir.mkdir(parents=True)
    bootloader_image = bootloader_dir / "bootloader-uart-bgapi.s37"
    bootloader_image.write_bytes(b"bootloader")
    dut_out_dir = build_root / "ncp_ble_dut_dcdc"
    dut_dir = dut_out_dir / "cmake_gcc" / "build" / "base"
    dut_dir.mkdir(parents=True)
    dut_image = dut_dir / "ncp_ble_dut_dcdc.s37"
    dut_image.write_bytes(b"dut")
    tester_out_dir = build_root / "ncp_ble_tester"
    tester_dir = tester_out_dir / "cmake_gcc" / "build" / "base"
    tester_dir.mkdir(parents=True)
    tester_image = tester_dir / "ncp_ble_tester.s37"
    tester_image.write_bytes(b"tester")
    runtime_dir = build_root / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime_xapi = runtime_dir / "sl_bt.xapi"
    runtime_xapi.write_bytes(b"xapi")
    sdk_path = Path(r"C:\sdk\2026.0.0")
    sdk_selection = app_manager.SDKSelection(
        path=sdk_path,
        version="2026.0.0",
        source="slt",
        commit="sdk-commit",
        sdk_hash="sdk-hash",
        xapi_path=sdk_path / app_manager.SL_BT_XAPI_PATH,
        xapi_hash=app_manager._sha256(runtime_xapi),
    )

    fw_data_path = app_manager.write_fw_data(
        build_root=build_root,
        firmware_cfg={
            "board": "brd4182c",
            "brev": "a06",
            "device": "EFR32MG22C224F512IM40",
            "sisdk": "2026.0.0",
        },
        sdk_path=sdk_path,
        runtime_xapi_path=runtime_xapi,
        summaries=[
            {
                "slcp": str(Path("apps/bootloader-uart-bgapi.slcp").resolve()),
                "out_dir": str(bootloader_out_dir.resolve()),
                "generated_slcp": str((bootloader_out_dir / "bootloader-uart-bgapi.slcp").resolve()),
                "image": str(bootloader_image.resolve()),
            },
            {
                "slcp": str(Path("apps/ncp_ble_dut_dcdc.slcp").resolve()),
                "out_dir": str(dut_out_dir.resolve()),
                "generated_slcp": str((dut_out_dir / "ncp_ble_dut_dcdc.slcp").resolve()),
                "image": str(dut_image.resolve()),
            },
            {
                "slcp": str(Path("apps/ncp_ble_tester.slcp").resolve()),
                "out_dir": str(tester_out_dir.resolve()),
                "generated_slcp": str((tester_out_dir / "ncp_ble_tester.slcp").resolve()),
                "image": str(tester_image.resolve()),
            }
        ],
        sdk_selection=sdk_selection,
    )

    fw_data = json.loads(fw_data_path.read_text(encoding="utf-8"))
    assert fw_data["firmware"] == {
        "board": "brd4182c",
        "brev": "a06",
        "device": "EFR32MG22C224F512IM40",
        "sisdk": "2026.0.0",
        "dut_app_hash": app_manager._sha256(dut_image),
        "tester_app_hash": app_manager._sha256(tester_image),
        "bootloader_app_hash": app_manager._sha256(bootloader_image),
    }
    assert fw_data["sdk"] == {
        "source": "slt",
        "version": "2026.0.0",
        "package_path": str(sdk_path.resolve()),
        "commit": "sdk-commit",
        "hash": "sdk-hash",
        "xapi_sha256": app_manager._sha256(runtime_xapi),
    }


def test_load_effective_config_layers_role_specific_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    config_root = Path("build/test_config_loader")
    if config_root.exists():
        shutil.rmtree(config_root)
    config_root.mkdir(parents=True)
    base_path = config_root / "dev_config.json"
    runtime_path = config_root / "cms_bench_runtime.json"
    build_path = config_root / "cms_client_config.json"
    base_path.write_text(
        json.dumps(
            {
                "tools": {"commander": "base-commander", "slc": "base-slc"},
                "firmware": {"board": "base-board", "sisdk": "2026.0.0"},
            }
        ),
        encoding="utf-8",
    )
    runtime_path.write_text('{"tools": {"commander": "runtime-commander",},}', encoding="utf-8")
    build_path.write_text(json.dumps({"tools": {"slc": "build-slc"}}), encoding="utf-8")

    monkeypatch.setenv(config_loader.DEFAULT_CONFIG_ENV, str(base_path))
    monkeypatch.setenv(config_loader.RUNTIME_CONFIG_ENV, str(runtime_path))
    monkeypatch.setenv(config_loader.BUILD_CONFIG_ENV, str(build_path))

    runtime_cfg = config_loader.load_effective_config(role="runtime")
    build_cfg = config_loader.load_effective_config(role="build")

    assert runtime_cfg["tools"]["commander"] == "runtime-commander"
    assert runtime_cfg["tools"]["slc"] == "base-slc"
    assert build_cfg["tools"]["slc"] == "build-slc"
    assert build_cfg["firmware"]["board"] == "base-board"


def test_main_clean_skips_firmware_validation_and_sisdk_resolution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app_manager, "resolve_build_root", lambda _: Path(".fw_build"))
    monkeypatch.setattr(app_manager, "clean_build_root", lambda path: 1)

    def fail_unexpected(*args, **kwargs):
        raise AssertionError("unexpected call during clean action")

    monkeypatch.setattr(app_manager, "validate_firmware_config", fail_unexpected)
    monkeypatch.setattr(app_manager, "resolve_sisdk_path", fail_unexpected)
    monkeypatch.setattr(app_manager, "trust_sisdk", fail_unexpected)

    exit_code = app_manager.main(["clean"])

    assert exit_code == 0
    assert "action=clean" in capsys.readouterr().out
