#!/usr/bin/env python3
"""Download the latest SiSDK ZIP archive from Silicon Labs Artifactory.

Usage:
    python scripts/download_sisdk.py 2026.12
    python scripts/download_sisdk.py 2026.12 12345
    python scripts/download_sisdk.py --list-versions
    python scripts/download_sisdk.py --list-builds 2026.12
    python scripts/download_sisdk.py --show-downloaded

Authentication is optional for publicly readable artifacts. For authenticated
access, set ARTIFACTORY_TOKEN, ARTIFACTORY_API_KEY, or both
ARTIFACTORY_USER and ARTIFACTORY_PASSWORD in the environment.
"""

import argparse
import base64
import hashlib
import json
import os
from enum import Enum, auto
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ARTIFACTORY_ROOT = "https://artifactory.silabs.net/artifactory"
REPOSITORY = "gsdk-generic-development"
ARCHIVE_NAME = "gecko-sdk.zip"
STATUS_FILENAME = "status.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".fw_build" / "sisdk"
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_BUFFER_SIZE = 2 * 1024 * 1024
SLIDING_WINDOW_MIN_SIZE = 1024 * 1024
SLIDING_WINDOW_MAX_SIZE = 256 * 1024 * 1024
SLIDING_WINDOW_N = 6


class SlidingWindowStatus(Enum):
    SUCCESSFUL_EXPAND = auto()
    SUCCESSFUL_MAINTAIN = auto()
    FAILED_REDUCE = auto()
    FAILED_ABORT = auto()


def SLIDING_WINDOW_ctrl(
    current_size: int,
    previous_size: int,
    previous_status: SlidingWindowStatus,
    *,
    min_size: int = SLIDING_WINDOW_MIN_SIZE,
    max_size: int = SLIDING_WINDOW_MAX_SIZE,
) -> int:
    """Choose the next read size from the outcome of the previous request."""
    if previous_status is SlidingWindowStatus.SUCCESSFUL_EXPAND:
        return min(current_size * 2, max_size)
    if previous_status is SlidingWindowStatus.FAILED_REDUCE:
        reduced_size = current_size // 2
        return max(min_size, min(reduced_size, previous_size))
    return current_size


class SlidingWindow:
    """Adapt read sizes and track throughput in memory for one operation."""

    def __init__(
        self,
        initial_size: int,
        *,
        min_size: int = SLIDING_WINDOW_MIN_SIZE,
        max_size: int = SLIDING_WINDOW_MAX_SIZE,
        n: int = SLIDING_WINDOW_N,
    ) -> None:
        self.min_size = max(1, min_size)
        self.max_size = max(self.min_size, max_size)
        self.n = max(2, n)
        self.current_size = self._bounded_size(initial_size)
        self.previous_size = self.current_size
        self.previous_status = SlidingWindowStatus.SUCCESSFUL_MAINTAIN
        self.successful_requests_at_size = 0
        self.failed_minimum_requests = 0
        self.total_bytes_read = 0
        self.total_read_seconds = 0.0
        self.last_requested_size_bytes = 0
        self.last_received_bytes = 0
        self.last_request_seconds = 0.0

    @property
    def requests_before_expand(self) -> int:
        return max(1, (self.n + 1) // 2)

    @property
    def is_aborted(self) -> bool:
        return self.failed_minimum_requests >= self.n

    def next_size(self) -> int:
        if self.is_aborted:
            raise RuntimeError("Sliding window aborted after repeated failures.")
        return self.current_size

    def record_read_success(
        self, requested_size: int, received_bytes: int, elapsed_seconds: float
    ) -> None:
        """Record bytes and elapsed time for one successful read request."""
        received_bytes = max(0, received_bytes)
        elapsed_seconds = max(0.0, elapsed_seconds)
        self.total_bytes_read += received_bytes
        self.total_read_seconds += elapsed_seconds
        self.last_requested_size_bytes = requested_size
        self.last_received_bytes = received_bytes
        self.last_request_seconds = elapsed_seconds

    @property
    def average_read_speed_bytes_per_second(self) -> float:
        if self.total_read_seconds <= 0:
            return 0.0
        return self.total_bytes_read / self.total_read_seconds

    @property
    def last_request_speed_bytes_per_second(self) -> float:
        if self.last_request_seconds <= 0:
            return 0.0
        return self.last_received_bytes / self.last_request_seconds

    def record_success(self) -> SlidingWindowStatus:
        self.previous_size = self.current_size
        self.failed_minimum_requests = 0
        self.successful_requests_at_size += 1
        if (
            self.successful_requests_at_size >= self.requests_before_expand
            and self.current_size < self.max_size
        ):
            self.successful_requests_at_size = 0
            self.previous_status = SlidingWindowStatus.SUCCESSFUL_EXPAND
            self.current_size = self._bounded_size(
                SLIDING_WINDOW_ctrl(
                    self.current_size,
                    self.previous_size,
                    self.previous_status,
                    min_size=self.min_size,
                    max_size=self.max_size,
                )
            )
        else:
            if self.current_size >= self.max_size:
                self.successful_requests_at_size = 0
            self.previous_status = SlidingWindowStatus.SUCCESSFUL_MAINTAIN
        return self.previous_status

    def record_failure(self) -> SlidingWindowStatus:
        self.successful_requests_at_size = 0
        if self.current_size <= self.min_size:
            self.failed_minimum_requests += 1
            if self.failed_minimum_requests >= self.n:
                self.previous_status = SlidingWindowStatus.FAILED_ABORT
                return self.previous_status
        self.failed_minimum_requests = (
            self.failed_minimum_requests if self.current_size <= self.min_size else 0
        )
        self.previous_status = SlidingWindowStatus.FAILED_REDUCE
        self.current_size = self._bounded_size(
            SLIDING_WINDOW_ctrl(
                self.current_size,
                self.previous_size,
                self.previous_status,
                min_size=self.min_size,
                max_size=self.max_size,
            )
        )
        return self.previous_status

    def _bounded_size(self, value: object) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError):
            size = self.min_size
        return min(self.max_size, max(self.min_size, size))


class ArtifactoryError(Exception):
    """Raised when Artifactory cannot provide the requested SDK artifact."""


def make_request(url: str) -> Request:
    request = Request(url, headers={"User-Agent": "pawr-network-testing/SiSDK-downloader"})

    token = os.environ.get("ARTIFACTORY_TOKEN")
    api_key = os.environ.get("ARTIFACTORY_API_KEY")
    username = os.environ.get("ARTIFACTORY_USER") or os.environ.get("ARTIFACTORY_USERNAME")
    password = os.environ.get("ARTIFACTORY_PASSWORD")

    if token:
        request.add_header("Authorization", f"Bearer {token}")
    elif api_key:
        request.add_header("X-JFrog-Art-Api", api_key)
    elif username and password:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {credentials}")
    elif username or password:
        raise ArtifactoryError(
            "Set both ARTIFACTORY_USER and ARTIFACTORY_PASSWORD, or use "
            "ARTIFACTORY_TOKEN / ARTIFACTORY_API_KEY."
        )

    return request


def open_artifact(url: str):
    try:
        return urlopen(make_request(url), timeout=120)
    except HTTPError as error:
        details = f"Artifactory returned HTTP {error.code} for {url}"
        if error.code in (401, 403):
            details += ". Check that your Artifactory credentials have read access."
        raise ArtifactoryError(details) from error
    except URLError as error:
        raise ArtifactoryError(f"Could not connect to Artifactory: {error.reason}") from error


def api_url(path: str, **query: str) -> str:
    encoded_path = quote(path.strip("/"), safe="/")
    url = f"{ARTIFACTORY_ROOT}/api/storage/{REPOSITORY}/{encoded_path}"
    if query:
        url += "?" + urlencode(query)
    return url


def download_url(path: str) -> str:
    encoded_path = quote(path.strip("/"), safe="/")
    return f"{ARTIFACTORY_ROOT}/{REPOSITORY}/{encoded_path}"


def get_json(url: str) -> dict:
    try:
        with open_artifact(url) as response:
            return json.load(response)
    except json.JSONDecodeError as error:
        raise ArtifactoryError(f"Artifactory returned invalid JSON from {url}") from error


def list_sdk_versions() -> list[str]:
    listing = get_json(api_url("", list="true", deep="0", listFolders="1"))
    entries = listing.get("files")
    if not isinstance(entries, list):
        raise ArtifactoryError("Artifactory's folder listing did not contain a 'files' list.")

    versions = set()
    for entry in entries:
        if not entry.get("folder"):
            continue
        name = str(entry.get("uri", "")).rstrip("/").rsplit("/", 1)[-1]
        match = re.fullmatch(r"sisdk-(\d{4}\.\d+)", name)
        if match:
            versions.add(match.group(1))

    return sorted(
        versions,
        key=lambda version: tuple(int(part) for part in version.split(".")),
    )


def list_builds(version: str) -> list[str]:
    version_path = f"sisdk-{version}"
    listing = get_json(api_url(version_path, list="true", deep="0", listFolders="1"))
    entries = listing.get("files")
    if not isinstance(entries, list):
        raise ArtifactoryError("Artifactory's folder listing did not contain a 'files' list.")

    builds = []
    for entry in entries:
        if not entry.get("folder"):
            continue
        name = str(entry.get("uri", "")).rstrip("/").rsplit("/", 1)[-1]
        if name.isdecimal():
            builds.append((int(name), name))

    builds.sort()
    return [name for _, name in builds]


def find_build(
    version: str,
    requested_build: str | None = None,
    *,
    fallback_to_latest: bool = False,
) -> str:
    builds = list_builds(version)
    if not builds:
        raise ArtifactoryError(
            f"No numeric build directories were found for SiSDK {version} in Artifactory."
        )

    if requested_build is None:
        return builds[-1]
    if requested_build not in builds:
        if fallback_to_latest:
            latest_build = builds[-1]
            print(
                f"SiSDK {version} build {requested_build} was not found; "
                f"using latest available build {latest_build}."
            )
            return latest_build
        raise ArtifactoryError(
            f"Build {requested_build} was not found for SiSDK {version} in Artifactory."
        )
    return requested_build


def list_downloaded_archives(output_root: Path) -> list[tuple[str, str, Path]]:
    if not output_root.is_dir():
        return []

    downloaded = []
    for archive in output_root.glob(f"*/*/{ARCHIVE_NAME}"):
        if not archive.is_file():
            continue
        version = archive.parent.parent.name
        build = archive.parent.name
        if re.fullmatch(r"\d{4}\.\d+", version) and build.isdecimal():
            downloaded.append((version, build, archive))

    return sorted(
        downloaded,
        key=lambda item: (
            tuple(int(part) for part in item[0].split(".")),
            int(item[1]),
            item[1],
        ),
    )


def load_sdk_status(status_path: Path) -> dict:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return status if isinstance(status, dict) else {}


def write_sdk_status(status_path: Path, status: dict) -> None:
    # Window tuning and read measurements are per-operation runtime state.
    for stage in ("download", "extraction"):
        stage_status = status.get(stage)
        if isinstance(stage_status, dict):
            stage_status.pop("SLIDING_WINDOW", None)
            stage_status.pop("spanning_window", None)

    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{status_path.name}.",
            suffix=".tmp",
            dir=status_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(status, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, status_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        for chunk in iter(lambda: archive.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_conan_lock(version: str, build: str, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict:
    """Download or reuse the lockfile for one exact SiSDK release and build."""
    if not re.fullmatch(r"\d{4}\.\d+", version) or not build.isdecimal():
        raise ValueError("SiSDK lockfile requires a release and numeric build")
    cache = output_root / version / build
    status_path = cache / STATUS_FILENAME
    status = load_sdk_status(status_path)
    if status.get("version") != version or status.get("build") != build:
        status = {}
    status.update({"schema_version": 1, "version": version, "build": build})
    lock_path = cache / "conan.lock"
    url = download_url(f"sisdk-{version}/{build}/conan.lock")
    entry = status.get("conan_lock", {})
    if (isinstance(entry, dict) and entry.get("status") == "complete"
            and entry.get("source") == url and lock_path.is_file()
            and entry.get("sha256") == _sha256_file(lock_path)):
        return {**entry, "path": str(lock_path.resolve())}
    try:
        with open_artifact(url) as response:
            content = response.read()
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("requires"), list):
            raise ValueError("lockfile has no requires list")
        cache.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(content)
        entry = {"status": "complete", "source": url, "sha256": _sha256_file(lock_path)}
    except (ArtifactoryError, OSError, ValueError) as error:
        entry = {"status": "unavailable", "source": url, "reason": str(error)}
    status["conan_lock"] = entry
    write_sdk_status(status_path, status)
    return {**entry, **({"path": str(lock_path.resolve())} if entry["status"] == "complete" else {})}


def _write_download_status(
    status_path: Path,
    version: str,
    build: str,
    download_status: dict,
) -> None:
    status = load_sdk_status(status_path)
    status.update(
        {
            "schema_version": 1,
            "version": version,
            "build": build,
        }
    )
    status.setdefault("extraction", {"status": "pending"})
    status["download"] = download_status
    write_sdk_status(status_path, status)


def download_archive(
    path: str,
    destination: Path,
    SLIDING_WINDOW: SlidingWindow,
) -> None:
    url = download_url(path)
    try:
        with open_artifact(url) as response, destination.open("wb") as archive:
            content_length = response.headers.get("Content-Length")
            try:
                total_bytes = int(content_length) if content_length is not None else None
            except ValueError:
                total_bytes = None
            if total_bytes is not None and total_bytes <= 0:
                total_bytes = None

            downloaded_bytes = 0
            last_percent = None
            while True:
                requested_size = SLIDING_WINDOW.next_size()
                read_started = time.perf_counter()
                try:
                    chunk = response.read(requested_size)
                except Exception as error:
                    SLIDING_WINDOW.record_failure()
                    raise ArtifactoryError(
                        f"SDK ZIP download read failed: {error}"
                    ) from error
                if not chunk:
                    break

                SLIDING_WINDOW.record_read_success(
                    requested_size, len(chunk), time.perf_counter() - read_started
                )
                archive.write(chunk)
                downloaded_bytes += len(chunk)
                SLIDING_WINDOW.record_success()
                if total_bytes is None:
                    print(
                        f"\rDownloaded {downloaded_bytes:,} bytes",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    percent = min(100, downloaded_bytes * 100 // total_bytes)
                    if percent != last_percent:
                        print(
                            f"\r{percent:3d}% ({downloaded_bytes:,}/{total_bytes:,} bytes)",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_percent = percent
            if downloaded_bytes:
                print(file=sys.stderr)
    except OSError as error:
        raise ArtifactoryError(f"Could not save the SDK ZIP archive: {error}") from error

    if total_bytes is not None and downloaded_bytes != total_bytes:
        SLIDING_WINDOW.record_failure()
        raise ArtifactoryError(
            f"SDK ZIP download ended early: received {downloaded_bytes:,} of {total_bytes:,} bytes."
        )


def download_sdk(
    version: str,
    output_root: Path,
    requested_build: str | None = None,
    *,
    fallback_to_latest: bool = False,
) -> Path:
    build = find_build(
        version,
        requested_build,
        fallback_to_latest=fallback_to_latest,
    )
    build_path = f"sisdk-{version}/{build}"
    archive_path = f"{build_path}/{ARCHIVE_NAME}"
    destination = output_root / version / build / ARCHIVE_NAME
    status_path = destination.parent / STATUS_FILENAME
    saved_status = load_sdk_status(status_path)
    download_status = saved_status.get("download")
    if not isinstance(download_status, dict):
        download_status = {}
    status_matches = (
        saved_status.get("version") == version
        and saved_status.get("build") == build
    )

    if destination.exists():
        if not destination.is_file():
            raise ArtifactoryError(
                f"The archive destination exists but is not a file: {destination}"
            )

        stored_sha256 = download_status.get("sha256", "") if status_matches else ""
        if download_status.get("status") == "complete" and stored_sha256:
            verification_started = time.perf_counter()
            actual_sha256 = _sha256_file(destination)
            verification_seconds = time.perf_counter() - verification_started
            if actual_sha256 == stored_sha256:
                original_seconds = download_status.get("duration_seconds")
                original_timing = (
                    f"; original download took {original_seconds:.2f}s"
                    if isinstance(original_seconds, (int, float))
                    else ""
                )
                print(
                    f"SiSDK {version} build {build} archive already exists; "
                    f"SHA-256 verified in {verification_seconds:.2f}s{original_timing}."
                )
                return destination

            print(
                f"SiSDK {version} build {build} archive SHA-256 does not match "
                "status.json; downloading a fresh copy."
            )
            destination.unlink()
        else:
            hash_started = time.perf_counter()
            actual_sha256 = _sha256_file(destination)
            hash_seconds = time.perf_counter() - hash_started
            _write_download_status(
                status_path,
                version,
                build,
                {
                    "status": "complete",
                    "archive": ARCHIVE_NAME,
                    "size_bytes": destination.stat().st_size,
                    "sha256": actual_sha256,
                    "duration_seconds": None,
                    "hash_seconds": round(hash_seconds, 3),
                    "adopted_existing_file": True,
                    "completed_at": time.time(),
                },
            )
            print(
                f"SiSDK {version} build {build} archive already exists; "
                f"recorded its SHA-256 in {hash_seconds:.2f}s."
            )
            return destination

    SLIDING_WINDOW = SlidingWindow(DOWNLOAD_BUFFER_SIZE)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{build}-", dir=destination.parent))
    try:
        archive = staging / ARCHIVE_NAME
        download_started_at = time.time()
        download_started = time.perf_counter()
        _write_download_status(
            status_path,
            version,
            build,
            {
                "status": "in_progress",
                "archive": ARCHIVE_NAME,
                "started_at": download_started_at,
            },
        )
        print(f"Downloading SiSDK {version}, build {build}...")
        transfer_started = time.perf_counter()
        try:
            download_archive(archive_path, archive, SLIDING_WINDOW)
        except ArtifactoryError as error:
            transfer_seconds = time.perf_counter() - transfer_started
            _write_download_status(
                status_path,
                version,
                build,
                {
                    "status": "failed",
                    "archive": ARCHIVE_NAME,
                    "duration_seconds": round(
                        time.perf_counter() - download_started, 3
                    ),
                    "transfer_seconds": round(transfer_seconds, 3),
                    "started_at": download_started_at,
                    "completed_at": time.time(),
                    "error": str(error),
                },
            )
            if SLIDING_WINDOW.is_aborted:
                raise ArtifactoryError(
                    f"SiSDK {version} build {build} download aborted after "
                    f"{SLIDING_WINDOW.n} failed requests at the minimum "
                    f"{SLIDING_WINDOW.min_size:,}-byte read size."
                ) from error
            archive_url = download_url(archive_path)
            if f"HTTP 404 for {archive_url}" in str(error):
                raise ArtifactoryError(
                    f"SiSDK {version} build {build} does not have {ARCHIVE_NAME} available yet."
                ) from error
            raise
        transfer_seconds = time.perf_counter() - transfer_started
        os.replace(archive, destination)
        hash_started = time.perf_counter()
        archive_sha256 = _sha256_file(destination)
        hash_seconds = time.perf_counter() - hash_started
        download_seconds = time.perf_counter() - download_started
        _write_download_status(
            status_path,
            version,
            build,
            {
                "status": "complete",
                "archive": ARCHIVE_NAME,
                "size_bytes": destination.stat().st_size,
                "sha256": archive_sha256,
                "duration_seconds": round(download_seconds, 3),
                "transfer_seconds": round(transfer_seconds, 3),
                "hash_seconds": round(hash_seconds, 3),
                "started_at": download_started_at,
                "completed_at": time.time(),
            },
        )
        print(
            f"SiSDK {version} build {build} downloaded in {transfer_seconds:.2f}s; "
            f"SHA-256 took {hash_seconds:.2f}s "
            f"(total {download_seconds:.2f}s; read window "
            f"{SLIDING_WINDOW.current_size / (1024 * 1024):g} MiB, "
            f"N={SLIDING_WINDOW.n}; average read speed "
            f"{SLIDING_WINDOW.average_read_speed_bytes_per_second / (1024 * 1024):.2f} MiB/s; "
            f"last chunk {SLIDING_WINDOW.last_received_bytes / (1024 * 1024):.2f} MiB "
            f"in {SLIDING_WINDOW.last_request_seconds:.3f}s at "
            f"{SLIDING_WINDOW.last_request_speed_bytes_per_second / (1024 * 1024):.2f} MiB/s)."
        )
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the ZIP archive for the latest or a specified SiSDK build."
    )
    parser.add_argument(
        "version",
        nargs="?",
        help="SiSDK version to download, for example 2026.12",
    )
    parser.add_argument(
        "build",
        nargs="?",
        help="Optional numeric build to download; defaults to the latest build",
    )
    listing_group = parser.add_mutually_exclusive_group()
    listing_group.add_argument(
        "--list-versions",
        action="store_true",
        help="List SiSDK versions available in Artifactory in ascending order",
    )
    listing_group.add_argument(
        "--list-builds",
        metavar="VERSION",
        help="List the 10 most recent numeric builds in ascending order",
    )
    listing_group.add_argument(
        "--show-downloaded",
        action="store_true",
        help="List SiSDK archives already present in the archive cache",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"SiSDK archive cache directory (default: {DEFAULT_OUTPUT_ROOT})",
    )
    args = parser.parse_args()
    if args.build and not args.build.isdecimal():
        parser.error("build must be a numeric build directory")
    if args.version and (args.list_builds or args.list_versions or args.show_downloaded):
        parser.error("do not provide a positional version with a listing option")

    version = args.list_builds or args.version
    if version and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version) or ".." in version
    ):
        parser.error("version must be a single safe path component, for example 2026.12")
    if not args.list_versions and not args.list_builds and not args.show_downloaded and not args.version:
        parser.error(
            "provide a version to download, --list-versions, --list-builds VERSION, "
            "or --show-downloaded"
        )
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.list_versions:
            versions = list_sdk_versions()
            if versions:
                print("SiSDK versions available in Artifactory:")
                for version in versions:
                    print(f"  {version}")
            else:
                print("No SiSDK versions were found in Artifactory.")
            return 0

        if args.list_builds:
            builds = list_builds(args.list_builds)
            if builds:
                recent_builds = builds[-10:]
                print(f"Last {len(recent_builds)} builds for SiSDK {args.list_builds}:")
                for build in recent_builds:
                    print(f"  {build}")
            else:
                print(f"No numeric builds were found for SiSDK {args.list_builds} in Artifactory.")
            return 0

        if args.show_downloaded:
            archives = list_downloaded_archives(args.output_root)
            if archives:
                print(f"Downloaded SiSDK archives in {args.output_root}:")
                for version, build, archive in archives:
                    print(f"  SiSDK {version} build {build}: {archive}")
            else:
                print(f"No downloaded SiSDK archives found in {args.output_root}.")
            return 0

        archive = download_sdk(args.version, args.output_root, args.build)
    except ArtifactoryError as error:
        print(f"Error: {error}")
        return 1

    print(f"Archive path: {archive.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
