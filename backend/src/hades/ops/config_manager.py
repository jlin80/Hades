"""Configuration Manager — export, version, diff, drift and import config.

Configuration in Hades is env-driven and immutable at runtime by design (the
:class:`Settings` aggregate is never rewritten in place). What was missing was a
way to *treat configuration as an auditable, versioned asset*: export the current
non-secret posture, snapshot it, diff two snapshots, detect drift from the last
snapshot, and import a snapshot as a new recorded version. This module adds
exactly that, on top of the append-only ``config_snapshots`` table.

Two hard rules:

* **Secrets never leave.** Every export is passed through :func:`redact`, which
  blanks any value whose key looks like a credential. A payload is redacted
  *before* it can be checksummed, snapshotted or returned by the API.
* **Import never mutates env or bypasses a guarded switch.** Importing records a
  new snapshot and audits the diff, and applies only *allow-listed runtime keys*
  to ``system_configuration``. Reserved keys (the trading mode, emergency mode)
  are refused — they change only through their own guarded services.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, select

from hades.contexts.audit.application.recorder import AuditRecorder
from hades.shared_kernel.config import Settings
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models import ConfigSnapshot, SystemConfiguration

_logger = get_logger("config.manager")

REDACTED = "<redacted>"
UNSET = "<unset>"

# A leaf value is redacted when its key contains any of these substrings.
_SENSITIVE: tuple[str, ...] = (
    "password",
    "secret",
    "webhook_url",
    "api_key",
    "auth_api_key",
    "keypair",
    "private_key",
    "token",  # e.g. discord bot token; note: overzealous by design (safe side)
)

# Keys that must never be written through an import — they have guarded owners.
_RESERVED_RUNTIME_KEYS: frozenset[str] = frozenset({"trading_mode", "emergency_mode"})


def redact(data: Any) -> Any:
    """Recursively blank secret-looking leaves. Non-secret values pass through."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and _is_sensitive(key) and not isinstance(value, dict | list):
                out[key] = REDACTED if value else UNSET
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE)


def export_settings(settings: Settings) -> dict[str, Any]:
    """Full, non-secret dump of the settings aggregate (secrets redacted)."""
    dumped = settings.model_dump(mode="json")
    result = redact(dumped)
    assert isinstance(result, dict)
    return result


def checksum(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical (sorted-key) JSON — stable and comparable."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Structured diff of two config payloads, keyed by dotted path."""
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, dict[str, Any]] = {}
    _walk_diff("", before, after, added, removed, changed)
    return {"added": added, "removed": removed, "changed": changed}


def _walk_diff(
    prefix: str,
    before: Any,
    after: Any,
    added: dict[str, Any],
    removed: dict[str, Any],
    changed: dict[str, dict[str, Any]],
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in before.keys() | after.keys():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in after:
                removed[path] = before[key]
            elif key not in before:
                added[path] = after[key]
            else:
                _walk_diff(path, before[key], after[key], added, removed, changed)
    elif before != after:
        changed[prefix] = {"from": before, "to": after}


# --- persistence ports -------------------------------------------------------


@runtime_checkable
class ConfigSnapshotStore(Protocol):
    async def add(
        self, version: int, payload: dict[str, Any], digest: str, created_by: str, note: str | None
    ) -> None: ...
    async def next_version(self) -> int: ...
    async def list(self) -> list[dict[str, Any]]: ...
    async def get(self, version: int) -> dict[str, Any] | None: ...
    async def latest(self) -> dict[str, Any] | None: ...


@runtime_checkable
class RuntimeConfigStore(Protocol):
    async def set(self, key: str, value: Any, actor: str) -> None: ...
    async def get(self, key: str) -> Any | None: ...
    async def all(self) -> dict[str, Any]: ...


# --- the manager -------------------------------------------------------------


class ConfigManager:
    """Exports, versions, diffs and imports the platform configuration."""

    def __init__(
        self,
        settings: Settings,
        *,
        snapshots: ConfigSnapshotStore,
        runtime: RuntimeConfigStore,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._snapshots = snapshots
        self._runtime = runtime
        self._audit = audit

    async def export(self) -> dict[str, Any]:
        """The current non-secret posture: env-driven settings + runtime overrides."""
        return {
            "settings": export_settings(self._settings),
            "runtime": redact(await self._runtime.all()),
        }

    async def snapshot(self, *, actor: str = "system", note: str | None = None) -> dict[str, Any]:
        payload = await self.export()
        digest = checksum(payload)
        version = await self._snapshots.next_version()
        await self._snapshots.add(version, payload, digest, actor, note)
        if self._audit is not None:
            await self._audit.log(
                actor=actor,
                action="config_snapshot_created",
                entity_type="configuration",
                after={"version": version, "checksum": digest, "note": note},
            )
        _logger.info("config_snapshot_created", version=version, checksum=digest)
        return {"version": version, "checksum": digest, "note": note}

    async def list_versions(self) -> list[dict[str, Any]]:
        return await self._snapshots.list()

    async def get_version(self, version: int) -> dict[str, Any] | None:
        return await self._snapshots.get(version)

    async def diff_versions(self, a: int, b: int) -> dict[str, Any]:
        left = await self._snapshots.get(a)
        right = await self._snapshots.get(b)
        if left is None or right is None:
            missing = [v for v, s in ((a, left), (b, right)) if s is None]
            raise ValueError(f"unknown config snapshot version(s): {missing}")
        return diff(left["payload"], right["payload"])

    async def detect_drift(self) -> dict[str, Any]:
        """Diff the live posture against the most recent snapshot."""
        latest = await self._snapshots.latest()
        current = await self.export()
        if latest is None:
            return {"drifted": True, "reason": "no snapshot yet", "diff": None}
        delta = diff(latest["payload"], current)
        drifted = bool(delta["added"] or delta["removed"] or delta["changed"])
        return {"drifted": drifted, "since_version": latest["version"], "diff": delta}

    async def import_config(self, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Adopt a snapshot as a new recorded version and apply runtime overrides.

        Validates the shape, records the diff vs the current posture, snapshots
        the imported payload, and writes only allow-listed runtime keys. Reserved
        keys are refused (they change only through their guarded services).
        """
        if not isinstance(payload, dict) or "settings" not in payload:
            raise ValueError("invalid config payload: expected an object with a 'settings' section")

        current = await self.export()
        delta = diff(current, payload)

        applied: list[str] = []
        refused: list[str] = []
        for key, value in dict(payload.get("runtime", {})).items():
            if key in _RESERVED_RUNTIME_KEYS:
                refused.append(key)
                continue
            await self._runtime.set(key, value, actor)
            applied.append(key)

        digest = checksum(payload)
        version = await self._snapshots.next_version()
        await self._snapshots.add(version, payload, digest, actor, "imported")

        if self._audit is not None:
            await self._audit.log(
                actor=actor,
                action="config_imported",
                entity_type="configuration",
                after={
                    "version": version,
                    "checksum": digest,
                    "applied_runtime_keys": applied,
                    "refused_runtime_keys": refused,
                    "diff": delta,
                },
            )
        _logger.warning("config_imported", version=version, applied=applied, refused=refused)
        return {"version": version, "applied": applied, "refused": refused, "diff": delta}


# --- Postgres adapters -------------------------------------------------------


class PostgresConfigSnapshotStore:
    """Append-only snapshot store over ``config_snapshots``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def add(
        self, version: int, payload: dict[str, Any], digest: str, created_by: str, note: str | None
    ) -> None:
        async with self._db.session() as session:
            session.add(
                ConfigSnapshot(
                    version=version,
                    payload=payload,
                    checksum=digest,
                    created_by=created_by,
                    note=note,
                )
            )

    async def next_version(self) -> int:
        async with self._db.session() as session:
            current = (
                await session.execute(select(func.max(ConfigSnapshot.version)))
            ).scalar_one_or_none()
        return int(current or 0) + 1

    async def list(self) -> list[dict[str, Any]]:
        stmt = select(ConfigSnapshot).order_by(ConfigSnapshot.version.desc())
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_snapshot_meta(row) for row in rows]

    async def get(self, version: int) -> dict[str, Any] | None:
        stmt = select(ConfigSnapshot).where(ConfigSnapshot.version == version)
        async with self._db.session() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _snapshot_full(row) if row is not None else None

    async def latest(self) -> dict[str, Any] | None:
        stmt = select(ConfigSnapshot).order_by(ConfigSnapshot.version.desc()).limit(1)
        async with self._db.session() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _snapshot_full(row) if row is not None else None


class PostgresRuntimeConfigStore:
    """Runtime key/value overrides over ``system_configuration``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def set(self, key: str, value: Any, actor: str) -> None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(SystemConfiguration).where(SystemConfiguration.key == key)
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    SystemConfiguration(
                        key=key, value={"value": value}, value_type="json", updated_by=actor
                    )
                )
            else:
                row.value = {"value": value}
                row.updated_by = actor

    async def get(self, key: str) -> Any | None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(SystemConfiguration).where(SystemConfiguration.key == key)
                )
            ).scalar_one_or_none()
        if row is None or not isinstance(row.value, dict):
            return None
        return row.value.get("value")

    async def all(self) -> dict[str, Any]:
        async with self._db.session() as session:
            rows = (await session.execute(select(SystemConfiguration))).scalars().all()
        out: dict[str, Any] = {}
        for row in rows:
            if row.is_secret:
                out[row.key] = REDACTED
            elif isinstance(row.value, dict) and "value" in row.value:
                out[row.key] = row.value["value"]
            else:
                out[row.key] = row.value
        return out


# --- in-memory adapters (tests / no-database dev) ----------------------------


class InMemoryConfigSnapshotStore:
    """In-memory append-only snapshot store."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    async def add(
        self, version: int, payload: dict[str, Any], digest: str, created_by: str, note: str | None
    ) -> None:
        self._rows.append(
            {
                "version": version,
                "payload": payload,
                "checksum": digest,
                "created_by": created_by,
                "note": note,
                "created_at": None,
            }
        )

    async def next_version(self) -> int:
        return (max((r["version"] for r in self._rows), default=0)) + 1

    async def list(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in r.items() if k != "payload"}
            for r in sorted(self._rows, key=lambda r: r["version"], reverse=True)
        ]

    async def get(self, version: int) -> dict[str, Any] | None:
        for r in self._rows:
            if r["version"] == version:
                return dict(r)
        return None

    async def latest(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        return dict(max(self._rows, key=lambda r: r["version"]))


class InMemoryRuntimeConfigStore:
    """In-memory runtime key/value overrides."""

    def __init__(self) -> None:
        self._kv: dict[str, Any] = {}

    async def set(self, key: str, value: Any, actor: str) -> None:
        self._kv[key] = value

    async def get(self, key: str) -> Any | None:
        return self._kv.get(key)

    async def all(self) -> dict[str, Any]:
        return dict(self._kv)


def _snapshot_meta(row: ConfigSnapshot) -> dict[str, Any]:
    return {
        "version": row.version,
        "checksum": row.checksum,
        "created_by": row.created_by,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _snapshot_full(row: ConfigSnapshot) -> dict[str, Any]:
    meta = _snapshot_meta(row)
    meta["payload"] = row.payload
    return meta
