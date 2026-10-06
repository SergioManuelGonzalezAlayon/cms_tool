import os
import re
from pathlib import Path


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


def _upload_file_name(path: Path, measured_at: str) -> str:
    timestamp = str(measured_at or "").strip()
    if not timestamp:
        raise ValueError("measured_at is required for the data-lake filename")
    timestamp = (
        timestamp.replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+0000", "Z")
        .replace("+00:00", "Z")
    )
    return f"{path.stem}_{timestamp}{path.suffix}"


def _safe_component(value: object, *, default: str = "unknown", allow_empty: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "" if allow_empty else default
    text = re.sub(r"[^a-z0-9._-]+", "_", text)
    text = text.strip("._-")
    if not text:
        return "" if allow_empty else default
    return text


def _record_upload_location(
    path: Path,
    *,
    cms_bench_id: object,
    sisdk_version: object,
    part_number: object,
    board: object,
    board_revision: object,
    serial_number: object,
    protocol: object,
    application_variant: object,
    test_case: object,
    measured_at: str,
) -> tuple[str, str]:
    components = [
        "cms",
        _safe_component(cms_bench_id),
        _safe_component(sisdk_version),
        _safe_component(part_number),
        f"{_safe_component(board)}_{_safe_component(board_revision)}",
        _safe_component(serial_number, allow_empty=True),
        _safe_component(protocol),
        _safe_component(application_variant),
        _safe_component(test_case),
    ]
    return _upload_file_name(path, measured_at), "/".join(components)


def upload_file_to_data_lake(
    local_file_path: str | Path,
    *,
    cms_bench_id: object,
    sisdk_version: object,
    part_number: object,
    board: object,
    board_revision: object,
    serial_number: object,
    protocol: object,
    application_variant: object,
    test_case: object,
    measured_at: str,
    tenant_key: str | None = None,
    tenant_key_file: str | Path | None = None,
):
    from datalake_client import send_file_to_data_lake

    path = Path(local_file_path)
    file_name, partition = _record_upload_location(
        path,
        cms_bench_id=cms_bench_id,
        sisdk_version=sisdk_version,
        part_number=part_number,
        board=board,
        board_revision=board_revision,
        serial_number=serial_number,
        protocol=protocol,
        application_variant=application_variant,
        test_case=test_case,
        measured_at=measured_at,
    )

    return send_file_to_data_lake(
        file_path=str(path),
        tenant_key=_tenant_key(tenant_key=tenant_key, tenant_key_file=tenant_key_file),
        file_name=file_name,
        compression=None,
        partition=partition,
        disable_date_partition=True,
    )


def upload_json_to_data_lake(
    local_json_path: str | Path,
    *,
    cms_bench_id: object,
    sisdk_version: object,
    part_number: object,
    board: object,
    board_revision: object,
    serial_number: object,
    protocol: object,
    application_variant: object,
    test_case: object,
    measured_at: str,
    tenant_key: str | None = None,
    tenant_key_file: str | Path | None = None,
):
    return upload_file_to_data_lake(
        local_json_path,
        cms_bench_id=cms_bench_id,
        sisdk_version=sisdk_version,
        part_number=part_number,
        board=board,
        board_revision=board_revision,
        serial_number=serial_number,
        protocol=protocol,
        application_variant=application_variant,
        test_case=test_case,
        measured_at=measured_at,
        tenant_key=tenant_key,
        tenant_key_file=tenant_key_file,
    )
