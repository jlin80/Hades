"""The container healthcheck must stay cheap and agree with the real settings.

Docker spawns a fresh interpreter for this module per service, per interval. When
it imported the whole configuration aggregate it cost more than a minute per
probe on a small host, so probes overlapped and piled up until six healthcheck
processes were the largest CPU consumer on the box — each one making the next
slower. These tests pin both halves of the fix: the module stays cheap to
import, and the constants it duplicates to stay cheap still match their source.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from hades.ops import healthcheck
from hades.ops.liveness import Liveness
from hades.shared_kernel.config.settings import ApiSettings, WatchdogSettings


def test_the_duplicated_defaults_match_the_real_settings() -> None:
    """The whole point of duplicating them is to avoid importing Settings."""
    watchdog = WatchdogSettings()
    assert watchdog.liveness_dir == healthcheck._DEFAULT_LIVENESS_DIR
    assert float(watchdog.liveness_max_age_seconds) == healthcheck._DEFAULT_MAX_AGE_SECONDS
    assert ApiSettings().port == healthcheck._DEFAULT_API_PORT


def test_it_agrees_with_the_liveness_writer_on_the_file_name(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Reader and writer must not drift apart on the path convention.

    The checker no longer goes through ``Liveness`` — it stats the path directly
    to avoid the import — so nothing but this test keeps the two in agreement.
    """
    monkeypatch.setenv("WATCHDOG_LIVENESS_DIR", str(tmp_path))
    written = Liveness("worker", directory=str(tmp_path))
    written.touch()

    assert written.path == tmp_path / "worker.alive"
    assert healthcheck._check_liveness("worker", max_age=60.0) == 0


def test_a_fresh_heartbeat_passes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WATCHDOG_LIVENESS_DIR", str(tmp_path))
    Liveness("worker", directory=str(tmp_path)).touch()

    assert healthcheck._check_liveness("worker", max_age=60.0) == 0


def test_a_stale_heartbeat_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WATCHDOG_LIVENESS_DIR", str(tmp_path))
    Liveness("worker", directory=str(tmp_path)).touch()
    stale = time.time() - 3600
    (tmp_path / "worker.alive").touch()
    import os

    os.utime(tmp_path / "worker.alive", (stale, stale))

    assert healthcheck._check_liveness("worker", max_age=60.0) == 1


def test_a_missing_heartbeat_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WATCHDOG_LIVENESS_DIR", str(tmp_path))

    assert healthcheck._check_liveness("never-started", max_age=60.0) == 1


def test_the_liveness_path_does_not_import_the_application() -> None:
    """The regression that mattered: cost, not correctness.

    Importing httpx, pydantic-settings and the settings tree is what made the
    probe slower than its own timeout. Run in a subprocess because this test
    process has already imported half the platform.
    """
    code = (
        "import sys; "
        "import hades.ops.healthcheck as h; "
        "h._check_liveness('nobody', 1.0); "
        "loaded = set(sys.modules); "
        "heavy = [m for m in ('httpx', 'pydantic_settings', 'sqlalchemy', 'fastapi') "
        "if m in loaded]; "
        "print(','.join(heavy))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "", f"liveness path pulled in: {result.stdout.strip()}"
