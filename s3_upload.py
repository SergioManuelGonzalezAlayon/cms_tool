import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from datalake_client import send_file_to_data_lake


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


def upload_json_to_data_lake(
    local_json_path: str,
    *,
    tenant_key: str | None = None,
    tenant_key_file: str | Path | None = None,
):
    path = Path(local_json_path)
    now = datetime.now(timezone.utc)

    return send_file_to_data_lake(
        file_path=str(path),
        tenant_key=_tenant_key(tenant_key=tenant_key, tenant_key_file=tenant_key_file),
        file_name=_upload_file_name(path, now),
        compression=None,
        partition=(
            f"{path.stem}/"
            f"date={now:%Y-%m-%d}/"
            f"hour={now:%H}"
        ),
        disable_date_partition=True,
    )


if __name__ == "__main__":
    print(upload_json_to_data_lake("./laura_test.json"))
