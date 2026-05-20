# CMS TODO

Captured on 2026-05-13.

## Priority items

1. Redesign SDK input handling so CMS can reproduce measurements against exact SiSDK snapshots from immutable CI or release artifacts. For each run we need to resolve the candidate SDK under test, the `main` or latest baseline SDK, and optionally the previous measured SDK so comparisons are made against known points rather than unknown distances. Each SDK must carry enough provenance to identify where it came from, which version it is, and which source commit is its head. The retrieval mechanism should be chosen only after this identity model is defined, because `SLT` alone is not enough and the database should not become the only source of truth.
2. Extend `apps/app_manager.py` so it can obtain firmware binaries from a URL, allowing users to point the tool at a remote app location.
3. Make the CMS run inside a container so a Raspberry Pi can act as a CMS Bench Manager, with enough host integration to control the DUT, the Tester, and the Joulescope.
4. Review the Validation outputs, especially the Excel-style table that was provided, and adjust the CMS result outputs so they are easier to find and easier to understand in the same style.
5. Add database integration. The likely target is `S3` plus `Redshift`, but the database does not exist yet, so implementation is currently blocked by the missing backend.
6. Define the correct `PRS` signaling for all supported devices, not only `xG22`. The `PRS` options should be taken from the `.slcp` files, moved into reusable files, and applied through the `slc --with` flow.
7. Investigate whether the Joulescope USB connection can be abstracted over Ethernet by using the Raspberry Pi as a USB-to-Ethernet bridge or switch point so HWMux can access the bench over the network instead of requiring direct local USB attachment. This is only viable if we can prove the capture path is stable under real test load. We need explicit testing for latency, jitter, dropped packets or frames, reconnect behavior, and whether any transport loss makes the current-measurement runs unstable or invalid.
