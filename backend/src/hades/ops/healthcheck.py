"""Container healthcheck — invoked by Docker's ``healthcheck`` directive.

Two modes:

- **HTTP** (default, the API): exits 0 if ``/health`` answers with a non-unhealthy
  status. Run as ``python -m hades.ops.healthcheck``.
- **Liveness file** (background services): exits 0 if the role's liveness file was
  touched within ``--max-age`` seconds. Run as
  ``python -m hades.ops.healthcheck --role watchdog``.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from hades.ops.liveness import Liveness
from hades.shared_kernel.config import get_settings


def _check_http() -> int:
    """Is *this API process* serving? Deliberately not "is the platform healthy".

    Docker restarts the container when this fails, so it must key off the one
    thing a restart can fix. ``/health`` reports an aggregate across Postgres,
    Redis, RPC and every background process — none of which a restart of the API
    repairs. Keying off that aggregate would let a slow RPC provider or a Redis
    blip put the API into a restart loop and turn a dependency hiccup into a real
    outage. Readiness gating on dependencies is ``/ready``'s job.
    """
    settings = get_settings()
    url = f"http://127.0.0.1:{settings.api.port}/health"
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
    settings = get_settings()
    liveness = Liveness(role, directory=settings.watchdog.liveness_dir)
    return 0 if liveness.is_fresh(max_age) else 1


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Hades container healthcheck")
    parser.add_argument("--role", default=None, help="background service role (liveness mode)")
    parser.add_argument(
        "--max-age",
        type=float,
        default=float(settings.watchdog.liveness_max_age_seconds),
        help="max liveness file age in seconds",
    )
    args = parser.parse_args()

    if args.role:
        return _check_liveness(args.role, args.max_age)
    return _check_http()


if __name__ == "__main__":
    sys.exit(main())
