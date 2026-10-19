"""Opt-in live upload probe for checking the bench's data-lake tenant key.

Run on the bench with:
    CURRENT_MEASUREMENT_RUN_LIVE_DATALAKE_UPLOAD_PROBE=1 \
        .venv/bin/python -m pytest -q utest/test_s3_upload_live.py

This uploads one small, uniquely named object and leaves it in the data lake.
"""

import logging
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from datalake_client import InvalidTenantKeyException

import s3_upload


_RUN_PROBE_ENV = "CURRENT_MEASUREMENT_RUN_LIVE_DATALAKE_UPLOAD_PROBE"


pytestmark = pytest.mark.skipif(
    os.environ.get(_RUN_PROBE_ENV) != "1",
    reason=f"Set {_RUN_PROBE_ENV}=1 to upload a live data-lake probe object",
)


def _redact_sensitive_diagnostics(value, tenant_key):
    message = str(value)
    if tenant_key:
        message = message.replace(tenant_key, "<redacted tenant key>")
    return re.sub(r"https?://[^\s\"'<>]+", "<redacted URL>", message)


def _upload_failure_message(exc, tenant_key):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(exc, InvalidTenantKeyException):
        status_code = status_code or 400
        response_body = str(exc)
    elif response is not None:
        response_body = getattr(response, "text", "") or str(exc)
    else:
        response_body = str(exc)

    message = [f"Data-lake upload probe failed: {type(exc).__name__}."]
    if status_code is not None:
        message.append(f"HTTP status: {status_code}")
        message.append(
            "HTTP response body: "
            + _redact_sensitive_diagnostics(response_body, tenant_key)[:4000]
        )
    else:
        message.append(
            "No HTTP response was available. Details: "
            + _redact_sensitive_diagnostics(response_body, tenant_key)[:4000]
        )
    return "\n".join(message)


def test_live_tenant_key_upload_probe(tmp_path, caplog):
    # The client logs exception tracebacks; the failure below reports the response directly.
    caplog.set_level(logging.CRITICAL)

    probe_id = uuid4().hex
    measured_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    probe_path = tmp_path / f"tenant_key_probe_{probe_id}.txt"
    probe_path.write_text("Current measurement data-lake tenant-key probe.\n", encoding="utf-8")

    bench_id = os.environ.get("CURRENT_MEASUREMENT_CMS_BENCH_ID", "").strip()
    tenant_key = None
    try:
        tenant_key = s3_upload._tenant_key()
        result = s3_upload.upload_file_to_data_lake(
            probe_path,
            cms_bench_id=bench_id or "tenant-key-probe",
            sisdk_version="diagnostic",
            part_number="diagnostic",
            board="diagnostic",
            board_revision="diagnostic",
            serial_number="",
            protocol="diagnostic",
            application_variant="probe",
            test_case="tenant-key-check",
            measured_at=measured_at,
            tenant_key=tenant_key,
        )
    except Exception as exc:
        pytest.fail(_upload_failure_message(exc, tenant_key), pytrace=False)

    assert result is not None
    assert result.key
