import pytest
import shlex
from pathlib import Path

from cms_client import build_cli_parser


def _help_text(*args: str) -> str:
    parser = build_cli_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*args, "--help"])
    assert exc.value.code == 0
    return parser.format_help() if not args else ""


def _command_help_text(*args: str, capsys) -> str:
    parser = build_cli_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*args, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_top_level_help_shows_normal_workflow(capsys):
    text = _command_help_text(capsys=capsys)

    assert "Normal workflow:" in text
    assert "cms health" in text
    assert "cms SEND_FW" in text
    assert "cms TEST --mode default --feature beacon --config-name default" in text
    assert "cms WATCH <measurement_id> --cursor <last_cursor>" in text
    assert "cms ARTIFACTS <measurement_id>" in text


def test_send_fw_help_explains_inputs_and_next_step(capsys):
    text = _command_help_text("SEND_FW", capsys=capsys)

    assert "--request-id" in text
    assert "--requested-by" in text
    assert "Example:" in text
    assert "Next:" in text
    assert "cms TEST" in text


def test_test_help_explains_config_preview_watch_and_results(capsys):
    text = _command_help_text("TEST", capsys=capsys)

    assert "mode" in text
    assert "feature" in text
    assert "--config-name" in text
    assert "--variable" in text
    assert "parameters.json" in text
    assert "cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview" in text
    assert "phy values coded, 1m, 2m" in text
    assert "--preview" in text
    assert "--upload-results-to-database" in text
    assert "Example:" in text
    assert "cms TEST --mode default --feature beacon --config-name dcdc --preview" in text
    assert "Next:" in text


def test_watch_help_explains_reconnect_cursor_and_verbose(capsys):
    text = _command_help_text("WATCH", capsys=capsys)

    assert "measurement_id" in text
    assert "--cursor" in text
    assert "--verbose" in text
    assert "cms WATCH <measurement_id> --cursor <last_cursor>" in text
    assert "Next:" in text


def test_cancel_and_artifacts_help_explain_scope(capsys):
    cancel_text = _command_help_text("CANCEL", capsys=capsys)
    artifacts_text = _command_help_text("ARTIFACTS", capsys=capsys)

    assert "active measurement" in cancel_text
    assert "cms WATCH" in cancel_text
    assert "measurement_id" in artifacts_text
    assert "explicit measurement" in artifacts_text
    assert "cms ARTIFACTS <measurement_id> --out-dir artifacts" in artifacts_text


def test_recipes_help_lists_focused_workflows(capsys):
    text = _command_help_text("RECIPES", capsys=capsys)

    assert "default config" in text
    assert "exact named config" in text
    assert "--feature" in text
    assert "--config-name" in text
    assert "--measurement-id" in text


def test_exit_codes_help_documents_stable_outcomes(capsys):
    text = _command_help_text("EXIT_CODES", capsys=capsys)

    assert "completed test failure" in text
    assert "connection failure" in text
    assert "protocol mismatch" in text
    assert "cms EXIT_CODES" in text


def test_failed_help_explains_rerun_workflow(capsys):
    text = _command_help_text("FAILED", capsys=capsys)

    assert "measurement_id" in text
    assert "--json" in text
    assert "orchestrator_results.jsonl" in text
    assert "rerun" in text
    assert "cms FAILED <measurement_id>" in text


def test_troubleshoot_help_lists_topics(capsys):
    text = _command_help_text("TROUBLESHOOT", capsys=capsys)

    assert "--topic" in text
    assert "health" in text
    assert "daemon" in text
    assert "truncated-output" in text
    assert "cms TROUBLESHOOT --topic health" in text


def test_documented_cms_commands_parse_successfully():
    parser = build_cli_parser()
    docs = Path("docs/cms_client.md").read_text(encoding="utf-8")
    commands = [
        line.strip()
        for line in docs.splitlines()
        if line.strip().startswith("cms ")
    ]

    assert commands
    for command in commands:
        normalized = (
            command.replace("<user>", "developer")
            .replace("<measurement_id>", "measurement-123")
            .replace("<last_cursor>", "17")
            .replace("<id>", "measurement-123")
            .replace("<mode>", "default")
            .replace("<feature>", "beacon")
            .replace("<name>", "default")
        )
        argv = shlex.split(normalized)[1:]
        parser.parse_args(argv)


def test_variable_filter_docs_use_declared_beacon_parameter_spelling():
    docs = Path("docs/cms_client.md").read_text(encoding="utf-8")

    assert "tests/configs/ble/beacon/parameters.json" in docs
    assert "`phy` with accepted values `coded`, `1m`, and `2m`" in docs
    assert "`dcdc` with values `On` and `Off`" in docs
    assert "cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview" in docs
    assert "cms TEST --mode full_sweep --feature beacon --variable phy=2m --variable dcdc=On --preview" in docs
    assert "cms FAILED <measurement_id> --json" in docs
    assert "--color auto|always|never" in docs
