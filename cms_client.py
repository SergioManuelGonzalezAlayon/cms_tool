"""Client-side helpers for talking to one CMS bench daemon.

`cms_client.py` is the remote control surface used by developers, CI, or other
automation. It speaks the daemon protocol and leaves bench-local hardware
control to `cmsd.py` and `cms_bench.py`.
"""

from __future__ import annotations

import platform

from pathlib import Path
from cms_protocol import BenchHealthReport, BenchStatus, MeasurementRequest, MeasurementResult


class CMSClient:
    """Remote client for one CMS bench daemon endpoint."""

    def __init__(self, base_url: str, api_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

    def get_health(self) -> BenchHealthReport:
        """Fetch the daemon health view for operator and CI checks."""
        raise NotImplementedError

    def get_status(self) -> BenchStatus:
        """Ask whether the bench is idle, busy, offline, or in error."""
        raise NotImplementedError

    def submit_measurement(self, request: MeasurementRequest) -> MeasurementResult:
        """Send one measurement request to the daemon and receive an accepted/busy reply."""
        raise NotImplementedError

    def get_measurement(self, measurement_id: str) -> MeasurementResult:
        """Fetch the current state and result summary for one submitted measurement."""
        raise NotImplementedError

    def wait_for_measurement(
        self,
        measurement_id: str,
        poll_interval_s: float = 5.0,
    ) -> MeasurementResult:
        """Poll the daemon until the target measurement reaches a terminal state."""
        raise NotImplementedError

    def cancel_measurement(self, measurement_id: str) -> MeasurementResult:
        """Request cancellation for the active measurement when the daemon allows it."""
        raise NotImplementedError

    def download_artifact(self, measurement_id: str, artifact_name: str, out_dir: Path) -> Path:
        """Copy one published result artifact from the daemon to the client host."""
        raise NotImplementedError

if __name__ == "__main__":

    cms_cli = CMSClient(
        base_url    = platform.node(),
        api_token   = "silabs-cms"
    )