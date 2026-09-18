"""SVD source resolution helpers for CMS Client build-time metadata."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import quote
from urllib.request import urlopen


ARTIFACTORY_SVD_ROOT = (
    "https://artifactory.silabs.net/ui/repos/tree/General/"
    "platmtl-generic-production/ide_export"
)
ARTIFACTORY_SVD_DOWNLOAD_ROOT = (
    "https://artifactory.silabs.net/artifactory/"
    "platmtl-generic-production/ide_export"
)
SVD_SOURCE_LOCAL = "local"
SVD_SOURCE_CACHE = "cache"
SVD_SOURCE_ARTIFACTORY = "artifactory"

_EFR32_DEVICE_FAMILY_RE = re.compile(r"^(?P<family>EFR32[A-Z]+[0-9]+)")
_EFR32_SVD_DEVICE_RE = re.compile(
    r"^(?P<family>EFR32[A-Z]+[0-9]+)(?P<revision>[A-Z])(?P<rest>[^-]*)(?:-.+)?$"
)
_SISDK_RELEASE_VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+)\.\d+(?:[._+-].*)?$")


@dataclass(frozen=True, slots=True)
class SvdSource:
    """Resolved source contract for one device SVD."""

    svd_version: str
    svd_device: str
    device_family: str
    artifactory_url: str
    download_url: str
    cache_path: Path
    source: str
    path: Path | None = None

    @property
    def is_available_locally(self) -> bool:
        return self.path is not None


@dataclass(frozen=True, slots=True)
class GpioPortRegisters:
    """Resolved GPIO register addresses for one port in an SVD."""

    port: str
    peripheral: str
    peripheral_base_address: int
    port_base_address: int
    dout_address: int | None = None
    pin_mask: int = 0
    mode_register_address: int | None = None
    mode_mask: int = 0
    mode_push_pull_value: int = 0
    dout_set_address: int | None = None
    dout_clear_address: int | None = None


def device_family_from_device(device: str) -> str:
    """Return the Artifactory SVD family directory for a device part number."""

    normalized = _required_identifier(device, "device").upper()
    match = _EFR32_DEVICE_FAMILY_RE.match(normalized)
    if match is None:
        raise ValueError(f"cannot derive device family from device: {device}")
    return match.group("family")


def svd_device_from_target_device(device: str) -> str:
    """Return the SVD filename device for a target part number."""

    normalized = _required_identifier(device, "device").upper()
    match = _EFR32_SVD_DEVICE_RE.match(normalized)
    if match is None:
        raise ValueError(f"cannot derive SVD device from target device: {device}")
    return f"{match.group('family')}A{match.group('rest')}"


def svd_export_version_from_sisdk_version(sisdk_version: str) -> str:
    """Return the Artifactory SVD export version for a SiSDK release."""

    version_id = _required_identifier(sisdk_version, "sisdk_version")
    match = _SISDK_RELEASE_VERSION_RE.match(version_id)
    if match is None:
        raise ValueError(f"cannot derive SVD export version from SiSDK version: {sisdk_version}")
    return f"{match.group('base')}.0"


def artifactory_svd_url(
    svd_version: str,
    svd_device: str,
    device_family: str = "",
) -> str:
    """Build the expected Artifactory browser URL for a device SVD."""

    version_id = _required_identifier(svd_version, "svd_version")
    device_id = _required_identifier(svd_device, "svd_device").upper()
    family_id = (
        _required_identifier(device_family, "device_family").upper()
        if device_family
        else device_family_from_device(device_id)
    )
    path_parts = [
        version_id,
        "iar",
        "SiliconLabs",
        "arm",
        "config",
        "debugger",
        "SiliconLaboratories",
        family_id,
        f"{device_id}.svd",
    ]
    return ARTIFACTORY_SVD_ROOT + "/" + "/".join(quote(part, safe="") for part in path_parts)


def artifactory_svd_download_url(
    svd_version: str,
    svd_device: str,
    device_family: str = "",
) -> str:
    """Build the expected raw Artifactory URL for downloading a device SVD."""

    version_id = _required_identifier(svd_version, "svd_version")
    device_id = _required_identifier(svd_device, "svd_device").upper()
    family_id = (
        _required_identifier(device_family, "device_family").upper()
        if device_family
        else device_family_from_device(device_id)
    )
    path_parts = [
        version_id,
        "iar",
        "SiliconLabs",
        "arm",
        "config",
        "debugger",
        "SiliconLaboratories",
        family_id,
        f"{device_id}.svd",
    ]
    return ARTIFACTORY_SVD_DOWNLOAD_ROOT + "/" + "/".join(
        quote(part, safe="") for part in path_parts
    )


def svd_cache_path(
    cache_dir: str | Path,
    svd_version: str,
    svd_device: str,
    device_family: str = "",
) -> Path:
    """Return the cache location for one resolved device SVD."""

    version_id = _required_identifier(svd_version, "svd_version")
    device_id = _required_identifier(svd_device, "svd_device").upper()
    family_id = (
        _required_identifier(device_family, "device_family").upper()
        if device_family
        else device_family_from_device(device_id)
    )
    return Path(cache_dir) / version_id / family_id / f"{device_id}.svd"


def resolve_svd_source(
    *,
    svd_version: str,
    svd_device: str,
    cache_dir: str | Path,
    device_family: str = "",
    configured_svd_path: str | Path | None = None,
) -> SvdSource:
    """Resolve the local/cache/Artifactory source contract for a device SVD.

    This function does not download from Artifactory. It makes the build-time
    contract explicit so the CMS Client can decide whether a local file is
    already available or where the SVD must be fetched from in a later step.
    """

    version_id = _required_identifier(svd_version, "svd_version")
    device_id = _required_identifier(svd_device, "svd_device").upper()
    family_id = (
        _required_identifier(device_family, "device_family").upper()
        if device_family
        else device_family_from_device(device_id)
    )
    cache_path = svd_cache_path(cache_dir, version_id, device_id, family_id)
    url = artifactory_svd_url(version_id, device_id, family_id)
    download_url = artifactory_svd_download_url(version_id, device_id, family_id)

    if configured_svd_path is not None and str(configured_svd_path).strip():
        local_path = Path(configured_svd_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"configured SVD path does not exist: {local_path}")
        return SvdSource(
            svd_version=version_id,
            svd_device=device_id,
            device_family=family_id,
            artifactory_url=url,
            download_url=download_url,
            cache_path=cache_path,
            source=SVD_SOURCE_LOCAL,
            path=local_path,
        )

    if cache_path.is_file():
        return SvdSource(
            svd_version=version_id,
            svd_device=device_id,
            device_family=family_id,
            artifactory_url=url,
            download_url=download_url,
            cache_path=cache_path,
            source=SVD_SOURCE_CACHE,
            path=cache_path,
        )

    return SvdSource(
        svd_version=version_id,
        svd_device=device_id,
        device_family=family_id,
        artifactory_url=url,
        download_url=download_url,
        cache_path=cache_path,
        source=SVD_SOURCE_ARTIFACTORY,
    )


def ensure_svd_source(
    *,
    svd_version: str,
    svd_device: str,
    cache_dir: str | Path,
    device_family: str = "",
    configured_svd_path: str | Path | None = None,
    downloader: Callable[[str, Path], None] | None = None,
) -> SvdSource:
    """Return a local SVD source, downloading from Artifactory on cache miss."""

    source = resolve_svd_source(
        svd_version=svd_version,
        svd_device=svd_device,
        cache_dir=cache_dir,
        device_family=device_family,
        configured_svd_path=configured_svd_path,
    )
    if source.path is not None:
        return source

    download = downloader or download_svd
    download(source.download_url, source.cache_path)
    if not source.cache_path.is_file():
        raise FileNotFoundError(f"downloaded SVD file was not created: {source.cache_path}")
    return SvdSource(
        svd_version=source.svd_version,
        svd_device=source.svd_device,
        device_family=source.device_family,
        artifactory_url=source.artifactory_url,
        download_url=source.download_url,
        cache_path=source.cache_path,
        source=SVD_SOURCE_ARTIFACTORY,
        path=source.cache_path,
    )


def download_svd(url: str, destination: str | Path) -> None:
    """Download one SVD file to the cache path."""

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(destination_path.name + ".tmp")
    try:
        with urlopen(url, timeout=60) as response, temporary_path.open("wb") as output:
            output.write(response.read())
        temporary_path.replace(destination_path)
    except Exception as exc:
        if temporary_path.exists():
            temporary_path.unlink()
        raise RuntimeError(
            f"Unable to download SVD from {url} to {destination_path}"
        ) from exc


def parse_gpio_port_registers_from_svd(
    svd_xml: str,
    port: str,
    pin: int | None = None,
) -> GpioPortRegisters:
    """Parse GPIO register metadata for one GPIO port."""

    port_id = _required_gpio_port(port)
    pin_id = _optional_gpio_pin(pin)
    root = ElementTree.fromstring(svd_xml)
    gpio = _find_gpio_peripheral(root)
    peripheral = _child_text(gpio, "name")
    peripheral_base = _parse_int(
        _required_child_text(gpio, "baseAddress", f"{peripheral} baseAddress")
    )

    cluster = _find_gpio_port_cluster(gpio, port_id)
    if cluster is not None:
        cluster_offset = _cluster_offset_for_port(cluster, port_id)
        port_base = peripheral_base + cluster_offset
        return GpioPortRegisters(
            port=port_id,
            peripheral=peripheral,
            peripheral_base_address=peripheral_base,
            port_base_address=port_base,
            dout_set_address=port_base
            + _register_offset_in_container(cluster, "DOUTSET"),
            dout_clear_address=port_base
            + _register_offset_in_container(cluster, "DOUTCLR"),
            pin_mask=_pin_mask(pin_id),
        )
    if _descendants(_child(gpio, "registers") or gpio, "cluster"):
        raise ValueError(f"GPIO port {port_id} cluster not found")

    return _parse_direct_gpio_port_registers(gpio, port_id, peripheral_base, pin_id)


def parse_gpio_port_registers_from_svd_file(
    path: str | Path,
    port: str,
    pin: int | None = None,
) -> GpioPortRegisters:
    """Read an SVD file and parse GPIO register metadata for one port."""

    return parse_gpio_port_registers_from_svd(
        Path(path).read_text(encoding="utf-8-sig"),
        port,
        pin,
    )


def _required_identifier(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if "/" in normalized or "\\" in normalized:
        raise ValueError(f"{name} must not contain path separators: {value}")
    return normalized


def _required_gpio_port(port: str) -> str:
    normalized = port.strip().upper()
    if not re.fullmatch(r"[A-Z]", normalized):
        raise ValueError(f"GPIO port must be a single letter: {port}")
    return normalized


def _optional_gpio_pin(pin: int | None) -> int | None:
    if pin is None:
        return None
    if pin < 0:
        raise ValueError(f"GPIO pin must be non-negative: {pin}")
    return pin


def _pin_mask(pin: int | None) -> int:
    return 0 if pin is None else 1 << pin


def _find_gpio_peripheral(root: ElementTree.Element) -> ElementTree.Element:
    for name in ("GPIO_NS", "GPIO", "GPIO_S"):
        peripheral = _find_peripheral_or_none(root, name)
        if peripheral is not None:
            return peripheral
    raise ValueError("SVD peripheral not found: GPIO_NS, GPIO, or GPIO_S")


def _find_peripheral(root: ElementTree.Element, name: str) -> ElementTree.Element:
    peripheral = _find_peripheral_or_none(root, name)
    if peripheral is not None:
        return peripheral
    raise ValueError(f"SVD peripheral not found: {name}")


def _find_peripheral_or_none(
    root: ElementTree.Element,
    name: str,
) -> ElementTree.Element | None:
    for peripheral in _descendants(root, "peripheral"):
        if _child_text(peripheral, "name") == name:
            return peripheral
    return None


def _find_gpio_port_cluster(
    gpio: ElementTree.Element,
    port: str,
) -> ElementTree.Element | None:
    for cluster in _descendants(gpio, "cluster"):
        name = _child_text(cluster, "name")
        dim_index = _dim_index_values(_child_text(cluster, "dimIndex"))
        if name == f"P{port}" or name == f"PORT{port}":
            return cluster
        if "%s" in name and port in dim_index:
            return cluster
    return None


def _cluster_offset_for_port(cluster: ElementTree.Element, port: str) -> int:
    offset = _parse_int(_child_text(cluster, "addressOffset") or "0")
    name = _child_text(cluster, "name")
    dim_index = _dim_index_values(_child_text(cluster, "dimIndex"))
    if "%s" not in name:
        return offset
    if port not in dim_index:
        raise ValueError(f"GPIO port {port} is not declared in cluster dimIndex")
    dim_increment = _parse_int(
        _required_child_text(cluster, "dimIncrement", "GPIO port cluster dimIncrement")
    )
    return offset + (dim_index.index(port) * dim_increment)


def _register_offset_in_container(container: ElementTree.Element, register_name: str) -> int:
    for register in _children(_child(container, "registers") or container, "register"):
        if _child_text(register, "name") == register_name:
            return _parse_int(
                _required_child_text(register, "addressOffset", f"{register_name} addressOffset")
            )
    raise ValueError(f"GPIO register not found: {register_name}")


def _parse_direct_gpio_port_registers(
    gpio: ElementTree.Element,
    port: str,
    peripheral_base: int,
    pin: int | None,
) -> GpioPortRegisters:
    registers = _child(gpio, "registers")
    if registers is None:
        raise ValueError(f"GPIO port {port} cluster not found")
    peripheral = _child_text(gpio, "name")
    port_base = peripheral_base + _direct_register_offset(registers, port, "CTRL")
    dout_offset = _direct_register_offset_or_none(registers, port, "DOUT")
    dout_set_offset = _direct_register_offset_or_none(registers, port, "DOUTSET")
    dout_clear_offset = _direct_register_offset_or_none(registers, port, "DOUTCLR")
    mode_register_offset = None
    mode_mask = 0
    mode_push_pull_value = 0
    if pin is not None:
        mode_register_name = "MODEL" if pin < 8 else "MODEH"
        mode_register = _direct_register(registers, port, mode_register_name)
        mode_register_offset = _parse_int(
            _required_child_text(
                mode_register,
                "addressOffset",
                f"PORT{port}_{mode_register_name} addressOffset",
            )
        )
        mode_shift = (pin if pin < 8 else pin - 8) * 4
        mode_mask = 0xF << mode_shift
        mode_push_pull_value = _gpio_mode_value(mode_register, f"MODE{pin % 8}", "PUSHPULL")
        mode_push_pull_value <<= mode_shift
    if dout_offset is None and (dout_set_offset is None or dout_clear_offset is None):
        raise ValueError(f"GPIO DOUT register not found for port {port}")
    return GpioPortRegisters(
        port=port,
        peripheral=peripheral,
        peripheral_base_address=peripheral_base,
        port_base_address=port_base,
        dout_address=peripheral_base + dout_offset if dout_offset is not None else None,
        pin_mask=_pin_mask(pin),
        mode_register_address=(
            peripheral_base + mode_register_offset if mode_register_offset is not None else None
        ),
        mode_mask=mode_mask,
        mode_push_pull_value=mode_push_pull_value,
        dout_set_address=peripheral_base + dout_set_offset if dout_set_offset is not None else None,
        dout_clear_address=(
            peripheral_base + dout_clear_offset if dout_clear_offset is not None else None
        ),
    )


def _direct_register_offset(registers: ElementTree.Element, port: str, register_name: str) -> int:
    register = _direct_register(registers, port, register_name)
    return _parse_int(
        _required_child_text(register, "addressOffset", f"{register_name} addressOffset")
    )


def _direct_register_offset_or_none(
    registers: ElementTree.Element,
    port: str,
    register_name: str,
) -> int | None:
    register = _direct_register_or_none(registers, port, register_name)
    if register is None:
        return None
    return _parse_int(
        _required_child_text(register, "addressOffset", f"{register_name} addressOffset")
    )


def _direct_register(
    registers: ElementTree.Element,
    port: str,
    register_name: str,
) -> ElementTree.Element:
    register = _direct_register_or_none(registers, port, register_name)
    if register is None:
        raise ValueError(f"GPIO register not found for port {port}: {register_name}")
    return register


def _direct_register_or_none(
    registers: ElementTree.Element,
    port: str,
    register_name: str,
) -> ElementTree.Element | None:
    accepted_names = {
        f"P{port}_{register_name}",
        f"PORT{port}_{register_name}",
        f"{register_name}{port}",
    }
    for register in _children(registers, "register"):
        if _child_text(register, "name") in accepted_names:
            return register
    return None


def _gpio_mode_value(
    mode_register: ElementTree.Element,
    field_name: str,
    value_name: str,
) -> int:
    fields = _child(mode_register, "fields")
    if fields is None:
        raise ValueError(f"GPIO mode register fields not found: {field_name}")
    for field in _children(fields, "field"):
        if _child_text(field, "name") != field_name:
            continue
        enumerated_values = _child(field, "enumeratedValues")
        if enumerated_values is None:
            break
        for value in _children(enumerated_values, "enumeratedValue"):
            if _child_text(value, "name") == value_name:
                return _parse_int(_required_child_text(value, "value", value_name))
    raise ValueError(f"GPIO mode value not found: {field_name}.{value_name}")


def _dim_index_values(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _required_child_text(element: ElementTree.Element, name: str, label: str) -> str:
    value = _child_text(element, name)
    if not value:
        raise ValueError(f"SVD field is missing: {label}")
    return value


def _child_text(element: ElementTree.Element, name: str) -> str:
    child = _child(element, name)
    return (child.text or "").strip() if child is not None else ""


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _descendants(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_int(value: str) -> int:
    cleaned = value.strip().replace("_", "")
    try:
        return int(cleaned, 0)
    except ValueError as exc:
        raise ValueError(f"SVD integer value is invalid: {value}") from exc
