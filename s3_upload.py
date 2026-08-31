import hashlib
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone


TENANT_KEY_ENV_VAR = "TENANT_KEY"
TENANT_KEY_FILE_ENV_VAR = "TENANT_KEY_FILE"
DEFAULT_TENANT_KEY_FILE = Path(__file__).resolve().with_name("S3Token.txt")


def _tenant_key(*, tenant_key: str | None = None, tenant_key_file: str | Path | None = None) -> str:
    if tenant_key:
        return tenant_key
    if os.environ.get(TENANT_KEY_ENV_VAR):
        return os.environ[TENANT_KEY_ENV_VAR]

    configured_token_file = tenant_key_file or os.environ.get(TENANT_KEY_FILE_ENV_VAR) or DEFAULT_TENANT_KEY_FILE
    path = Path(configured_token_file)
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key

    raise RuntimeError(
        f"Missing data lake tenant key. Set {TENANT_KEY_ENV_VAR} or provide {path}."
    )


def _upload_file_name(path: Path, now: datetime) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    timestamp = _record_timestamp(path) or now.strftime("%Y%m%dT%H%M%SZ")
    return f"{path.stem}_{timestamp}_{digest}{path.suffix}"


def _record_timestamp(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    measured_at = str(data.get("test_run", {}).get("measured_at", "")).strip()
    if not measured_at:
        return None
    return (
        measured_at.replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+0000", "Z")
        .replace("+00:00", "Z")
    )


def _safe_component(value: object, *, default: str = "unknown", allow_empty: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "" if allow_empty else default
    text = re.sub(r"[^a-z0-9._-]+", "_", text)
    text = text.strip("._-")
    if not text:
        return "" if allow_empty else default
    return text


def _config_path_storage_parts(config_path: object) -> tuple[str | None, str | None, str | None]:
    if not config_path:
        return None, None, None
    parts = Path(str(config_path)).parts
    if "configs" not in parts:
        return None, None, None
    index = parts.index("configs")
    protocol = parts[index + 1] if len(parts) > index + 1 else None
    test_case = parts[index + 2] if len(parts) > index + 2 else None
    partition = None
    if "full_sweep" in parts:
        full_sweep_index = parts.index("full_sweep")
        partition = parts[full_sweep_index + 1] if len(parts) > full_sweep_index + 1 else None
    return protocol, test_case, partition


def _record_upload_location(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        now = datetime.now(timezone.utc)
        return path.stem, f"cms/unknown/unknown/unknown/unknown/{now:%Y%m%dT%H%M%SZ}"

    storage = data.get("storage", {})
    dataset = data.get("dataset", {})
    test_run = data.get("test_run", {})
    firmware = data.get("firmware", {})
    protocol_from_path, test_case_from_path, partition_from_path = _config_path_storage_parts(
        test_run.get("config_path")
    )

    cms_bench_id = _safe_component(
        storage.get("cms_bench_id") or dataset.get("cms_bench_id")
    )
    protocol = _safe_component(
        storage.get("protocol") or protocol_from_path or dataset.get("wireless_stack")
    )
    test_case = _safe_component(
        storage.get("test_case") or test_case_from_path or test_run.get("test_name")
    )
    partition_name = _safe_component(
        storage.get("partition") or dataset.get("partition") or partition_from_path
    )
    part_number = _safe_component(
        storage.get("part_number")
        or firmware.get("part_number")
        or firmware.get("device")
        or dataset.get("part_number")
        or dataset.get("chip_id")
    )
    board = _safe_component(storage.get("board") or firmware.get("board") or dataset.get("board_id"))
    board_revision = _safe_component(
        storage.get("board_revision")
        or firmware.get("board_revision")
        or firmware.get("brev")
        or firmware.get("board_rev")
    )
    serial_number = _safe_component(
        storage.get("serial_number") or firmware.get("serial_number"),
        allow_empty=True,
    )
    config_name = _safe_component(
        storage.get("config_name") or test_run.get("config_name") or path.stem
    )
    board_dir = f"{board}_{board_revision}"

    partition = "/".join(
        [
            "cms",
            cms_bench_id,
            part_number,
            board_dir,
            serial_number,
            protocol,
            partition_name,
            test_case,
        ]
    )
    return f"{config_name}{path.suffix}", partition


def upload_json_to_data_lake(
    local_json_path: str,
    *,
    tenant_key: str | None = None,
    tenant_key_file: str | Path | None = None,
):
    from datalake_client import send_file_to_data_lake

    path = Path(local_json_path)
    file_name, partition = _record_upload_location(path)

    return send_file_to_data_lake(
        file_path=str(path),
        tenant_key=_tenant_key(tenant_key=tenant_key, tenant_key_file=tenant_key_file),
        file_name=file_name,
        compression=None,
        partition=partition,
        disable_date_partition=True,
    )


if __name__ == "__main__":
    print(upload_json_to_data_lake("./laura_test.json"))
