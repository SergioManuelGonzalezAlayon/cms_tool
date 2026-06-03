import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_NAME = "dev_config.json"
BUILD_CONFIG_NAME = "cms_client_config.json"
RUNTIME_CONFIG_NAME = "cms_bench_runtime.json"
DEFAULT_CONFIG_ENV = "CURRENT_MEASUREMENT_DEV_CONFIG_FILE"
BUILD_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_CLIENT_CONFIG_FILE"
RUNTIME_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_BENCH_RUNTIME_FILE"


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_explicit_path(env_name: str) -> Path | None:
    configured = os.getenv(env_name)
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Configured path in {env_name} does not exist: {path}")
    return path


def _default_path(filename: str) -> Path:
    return REPO_ROOT / filename


def get_effective_config_paths(*, role: str | None = None) -> list[Path]:
    paths: list[Path] = []

    base_path = _resolve_explicit_path(DEFAULT_CONFIG_ENV) or _default_path(DEFAULT_CONFIG_NAME)
    if base_path.exists():
        paths.append(base_path)

    if role == "build":
        overlay_path = _resolve_explicit_path(BUILD_CONFIG_ENV) or _default_path(BUILD_CONFIG_NAME)
        if overlay_path.exists():
            paths.append(overlay_path)
    elif role == "runtime":
        overlay_path = _resolve_explicit_path(RUNTIME_CONFIG_ENV) or _default_path(RUNTIME_CONFIG_NAME)
        if overlay_path.exists():
            paths.append(overlay_path)

    return paths


def load_effective_config(*, role: str | None = None) -> dict:
    config: dict = {}
    for path in get_effective_config_paths(role=role):
        config = _deep_merge(config, _read_json_file(path))
    return config

