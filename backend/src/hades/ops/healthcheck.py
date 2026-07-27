"""Container healthcheck — invoked by Docker's ``healthcheck`` directive.

Two modes:

- **HTTP** (default, the API): exits 0 if ``/health`` answers with a non-unhealthy
  status. Run as ``python -m hades.ops.healthcheck``.
- **Liveness file** (background services): exits 0 if the role's liveness file was
  touched within ``--max-age`` seconds. Run as
  ``python -m hades.ops.healthcheck --role watchdog``.

**This module must stay cheap to import.** It is not library code that runs once
in a long-lived process — Docker spawns a fresh interpreter for it, per service,
on every interval. It previously imported ``httpx`` unconditionally and built the
whole ``Settings`` tree (thirty-odd pydantic-settings sections, each re-reading
``.env``) just to learn a directory name and a number. On a small host that cost
more than a minute per probe, which meant probes overlapped, piled up, and became
the largest CPU consumer on the box — six healthcheck processes burning more than
the platform they were checking, each one making the next slower.

So: no module-level import of anything heavy, and the liveness path reads its two
values straight from the environment rather than constructing the configuration
aggregate. The defaults are duplicated from ``WatchdogSettings`` deliberately;
importing that module is the very cost being avoided, and the pair is pinned by a
test so the duplication cannot drift silently.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

#: Mirrors ``WatchdogSettings.liveness_dir`` / ``liveness_max_age_seconds`` and
#: ``ApiSettings.port``. Pinned against the real settings by a test.
_DEFAULT_LIVENESS_DIR = "/var/run/hades"
_DEFAULT_MAX_AGE_SECONDS = 60.0
_DEFAULT_API_PORT = 8000


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        return int(raw) if raw else default
    except ValueError:
        return default


def _check_http() -> int:
    """Is *this API process* serving? Deliberately not "is the platform healthy".

    Docker restarts the container when this fails, so it must key off the one
    thing a restart can fix. ``/health`` reports an aggregate across Postgres,
    Redis, RPC and every background process — none of which a restart of the API
    repairs. Keying off that aggregate would let a slow RPC provider or a Redis
    blip put the API into a restart loop and turn a dependency hiccup into a real
    outage. Readiness gating on dependencies is ``/ready``'s job.
    """
    # Imported here, not at module scope: the liveness path (five of the six
    # services) must not pay for an HTTP client it never uses.
    import httpx

    port = _env_int("API_PORT", _DEFAULT_API_PORT)
    url = f"http://127.0.0.1:{port}/health"
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError:
        return 1
    if resp.status_code != 200:
        return 1
    try:
        components = resp.json().get("components", [])
    except ValueError:
        return 1
    # Serving a well-formed body is itself most of the proof; the api component
    # is the explicit assertion about this process.
    for component in components:
        if component.get("name") == "api":
            return 0 if component.get("status") != "unhealthy" else 1
    return 0


def _check_liveness(role: str, max_age: float) -> int:
    """Is the role's heartbeat file fresh? A stat call, and nothing more."""
    directory = os.environ.get("WATCHDOG_LIVENESS_DIR") or _DEFAULT_LIVENESS_DIR
    path = Path(directory) / f"{role}.alive"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return 1  # missing or unreadable — the process has not checked in
    return 0 if age <= max_age else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hades container healthcheck")
    parser.add_argument("--role", default=None, help="background service role (liveness mode)")
    parser.add_argument(
        "--max-age",
        type=float,
        default=None,
        help="max liveness file age in seconds",
    )
    args = parser.parse_args()

    if args.role:
        max_age = args.max_age
        if max_age is None:
            max_age = _env_float("WATCHDOG_LIVENESS_MAX_AGE_SECONDS", _DEFAULT_MAX_AGE_SECONDS)
        return _check_liveness(args.role, max_age)
    return _check_http()


if __name__ == "__main__":
    sys.exit(main())
