from pathlib import Path

from utils.svd import (
    GpioPortRegisters,
    SVD_SOURCE_ARTIFACTORY,
    SVD_SOURCE_CACHE,
    SVD_SOURCE_LOCAL,
    artifactory_svd_download_url,
    artifactory_svd_url,
    device_family_from_device,
    ensure_svd_source,
    parse_gpio_port_registers_from_svd,
    resolve_svd_source,
    svd_cache_path,
    svd_device_from_target_device,
    svd_export_version_from_sisdk_version,
)


def test_svd_artifactory_url_and_cache_path_for_mg22(tmp_path: Path) -> None:
    svd_device = svd_device_from_target_device("EFR32MG22C224F512IM40-C")
    svd_version = svd_export_version_from_sisdk_version("2025.12.3")

    assert svd_device == "EFR32MG22A224F512IM40"
    assert svd_version == "2025.12.0"
    assert device_family_from_device(svd_device) == "EFR32MG22"
    assert artifactory_svd_url(
        svd_version,
        svd_device,
    ).endswith(
        "/2025.12.0/iar/SiliconLabs/arm/config/debugger/"
        "SiliconLaboratories/EFR32MG22/EFR32MG22A224F512IM40.svd"
    )
    assert artifactory_svd_download_url(
        svd_version,
        svd_device,
    ).endswith(
        "/artifactory/platmtl-generic-production/ide_export/"
        "2025.12.0/iar/SiliconLabs/arm/config/debugger/"
        "SiliconLaboratories/EFR32MG22/EFR32MG22A224F512IM40.svd"
    )
    assert svd_cache_path(
        tmp_path,
        svd_version,
        svd_device,
    ) == tmp_path / "2025.12.0" / "EFR32MG22" / "EFR32MG22A224F512IM40.svd"


def test_resolve_svd_source_prefers_local_then_cache_then_artifactory(tmp_path: Path) -> None:
    configured_svd = tmp_path / "provided.svd"
    configured_svd.write_text("<device />", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_path = svd_cache_path(cache_dir, "2025.12.3", "EFR32MG22A224F512IM40")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("<device />", encoding="utf-8")

    local_source = resolve_svd_source(
        svd_version="2025.12.3",
        svd_device="EFR32MG22A224F512IM40",
        cache_dir=cache_dir,
        configured_svd_path=configured_svd,
    )
    cache_source = resolve_svd_source(
        svd_version="2025.12.3",
        svd_device="EFR32MG22A224F512IM40",
        cache_dir=cache_dir,
    )
    artifactory_source = resolve_svd_source(
        svd_version="2025.12.3",
        svd_device="EFR32MG22A224F512IM40",
        cache_dir=tmp_path / "empty-cache",
    )

    assert local_source.source == SVD_SOURCE_LOCAL
    assert local_source.path == configured_svd
    assert cache_source.source == SVD_SOURCE_CACHE
    assert cache_source.path == cache_path
    assert artifactory_source.source == SVD_SOURCE_ARTIFACTORY
    assert artifactory_source.path is None


def test_ensure_svd_source_downloads_artifactory_cache_miss(tmp_path: Path) -> None:
    downloads: list[tuple[str, Path]] = []

    def fake_downloader(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.parent.mkdir(parents=True)
        destination.write_text("<device />", encoding="utf-8")

    source = ensure_svd_source(
        svd_version="2025.12.3",
        svd_device="EFR32MG22A224F512IM40",
        cache_dir=tmp_path,
        downloader=fake_downloader,
    )

    assert source.source == SVD_SOURCE_ARTIFACTORY
    assert source.path == tmp_path / "2025.12.3" / "EFR32MG22" / "EFR32MG22A224F512IM40.svd"
    assert downloads == [(source.download_url, source.cache_path)]


def test_parse_gpio_port_registers_from_direct_gpio_ns_svd() -> None:
    svd = """
    <device>
      <peripherals>
        <peripheral>
          <name>GPIO_NS</name>
          <baseAddress>0x5003C000</baseAddress>
          <registers>
            <register><name>PORTB_CTRL</name><addressOffset>0x030</addressOffset></register>
            <register>
              <name>PORTB_MODEL</name>
              <addressOffset>0x034</addressOffset>
              <fields>
                <field>
                  <name>MODE4</name>
                  <enumeratedValues>
                    <enumeratedValue><name>PUSHPULL</name><value>4</value></enumeratedValue>
                  </enumeratedValues>
                </field>
              </fields>
            </register>
            <register><name>PORTB_DOUT</name><addressOffset>0x040</addressOffset></register>
          </registers>
        </peripheral>
      </peripherals>
    </device>
    """

    assert parse_gpio_port_registers_from_svd(svd, "B", 4) == GpioPortRegisters(
        port="B",
        peripheral="GPIO_NS",
        peripheral_base_address=0x5003C000,
        port_base_address=0x5003C030,
        dout_address=0x5003C040,
        pin_mask=0x10,
        mode_register_address=0x5003C034,
        mode_mask=0x000F0000,
        mode_push_pull_value=0x00040000,
    )
