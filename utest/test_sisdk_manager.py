import io
import json
from pathlib import Path

import pytest

from apps import sisdk_manager


def test_lockfile_download_cache_reuse_and_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'{"version":"0.5","requires":["library/1.0#abc"]}'
    urls = []

    def download(url):
        urls.append(url)
        return io.BytesIO(content)

    monkeypatch.setattr(sisdk_manager, "open_artifact", download)
    first = sisdk_manager.download_conan_lock("2026.12", "1310", tmp_path)
    second = sisdk_manager.download_conan_lock("2026.12", "1310", tmp_path)
    assert len(urls) == 1
    assert urls[0].endswith("/sisdk-2026.12/1310/conan.lock")
    assert Path(second["path"]).read_bytes() == content
    status = json.loads((tmp_path / "2026.12/1310/status.json").read_text())
    assert status["conan_lock"]["sha256"] == first["sha256"]
    Path(first["path"]).write_bytes(b"corrupted")
    repaired = sisdk_manager.download_conan_lock("2026.12", "1310", tmp_path)
    assert len(urls) == 2
    assert Path(repaired["path"]).read_bytes() == content


def test_lockfile_invalid_response_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sisdk_manager, "open_artifact", lambda url: io.BytesIO(b'{"requires": null}'))
    result = sisdk_manager.download_conan_lock("2026.12", "1310", tmp_path)
    assert result["status"] == "unavailable"
    assert "requires" in result["reason"]
    assert "path" not in result
