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
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import zipfile


ARTIFACTORY_ROOT = "https://artifactory.silabs.net/artifactory"
REPOSITORY = "gsdk-generic-development"
ARCHIVE_NAME = "gecko-sdk.zip"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".fw_build" / "sisdk"
CHUNK_SIZE = 1024 * 1024


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


def find_build(version: str, requested_build: str | None = None) -> str:
    builds = list_builds(version)
    if not builds:
        raise ArtifactoryError(
            f"No numeric build directories were found for SiSDK {version} in Artifactory."
        )

    if requested_build is None:
        return builds[-1]
    if requested_build not in builds:
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


def get_checksums(path: str) -> dict:
    try:
        info = get_json(api_url(path))
    except ArtifactoryError:
        return {}

    checksums = info.get("checksums", {})
    if not isinstance(checksums, dict):
        return {}
    return {name.lower(): value.lower() for name, value in checksums.items() if value}


def download_archive(path: str, destination: Path, checksums: dict) -> None:
    url = download_url(path)
    hashes = {name: hashlib.new(name) for name in ("sha256", "sha1", "md5")}
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
            while chunk := response.read(CHUNK_SIZE):
                archive.write(chunk)
                downloaded_bytes += len(chunk)
                for digest in hashes.values():
                    digest.update(chunk)
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
        raise ArtifactoryError(
            f"SDK ZIP download ended early: received {downloaded_bytes:,} of {total_bytes:,} bytes."
        )

    for name, expected in checksums.items():
        if name in hashes and hashes[name].hexdigest() != expected:
            raise ArtifactoryError(f"Downloaded archive failed its Artifactory {name} checksum.")


def validate_archive(archive: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as package:
            bad_member = package.testzip()
            if bad_member:
                raise ArtifactoryError(f"SDK archive is corrupt at {bad_member}.")
    except zipfile.BadZipFile as error:
        raise ArtifactoryError("The downloaded SDK file is not a valid ZIP archive.") from error


def download_sdk(version: str, output_root: Path, requested_build: str | None = None) -> Path:
    build = find_build(version, requested_build)
    build_path = f"sisdk-{version}/{build}"
    archive_path = f"{build_path}/{ARCHIVE_NAME}"
    destination = output_root / version / build / ARCHIVE_NAME

    if destination.exists():
        if not destination.is_file():
            raise ArtifactoryError(
                f"The archive destination exists but is not a file: {destination}"
            )
        print(f"SiSDK {version} build {build} archive is already downloaded: {destination}")
        return destination

    checksums = get_checksums(archive_path)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{build}-", dir=destination.parent))
    try:
        archive = staging / ARCHIVE_NAME
        print(f"Downloading SiSDK {version}, build {build}...")
        try:
            download_archive(archive_path, archive, checksums)
        except ArtifactoryError as error:
            archive_url = download_url(archive_path)
            if f"HTTP 404 for {archive_url}" in str(error):
                raise ArtifactoryError(
                    f"SiSDK {version} build {build} does not have {ARCHIVE_NAME} available yet."
                ) from error
            raise
        if not checksums:
            print("Artifactory checksum metadata is unavailable; validating the ZIP archive itself.")
        validate_archive(archive)
        os.replace(archive, destination)
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
