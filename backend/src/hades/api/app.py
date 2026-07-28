"""FastAPI application factory.

Builds the :class:`Container` on startup, stores it on ``app.state`` for the
dependency helpers, wires routers and cross-cutting middleware, and disposes
resources on shutdown. Nothing here knows domain logic — routers delegate to the
command/query buses.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hades import __version__
from hades.api.errors import register_exception_handlers
from hades.api.routers import (
    audit,
    committee,
    config,
    execution,
    funnel,
    health,
    info,
    intelligence,
    knowledge,
    meta,
    metrics,
    portfolio,
    research,
    risk,
    scanner,
    security,
    status,
    strategy,
    trading,
    version,
)
from hades.api.ws import routes as ws_routes
from hades.bootstrap import Container, build_container
from hades.shared_kernel.config import Settings, get_settings
from hades.shared_kernel.logging import get_log_buffer, get_logger
from hades.shared_kernel.logging.setup import set_log_shipper
from hades.shared_kernel.logging.shipper import LogShipper, LogTailer

_logger = get_logger("api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    container = build_container(settings, role="api")
    app.state.container = container
    app.state.started_at = time.time()
    # The API both ships its own lines and tails everyone else's, so the
    # dashboard terminal shows the whole platform — above all the Worker,
    # which hosts the entire trading pipeline and was previously invisible.
    shipper, tailer = await _start_log_bridge(app, container, settings)
    _logger.info("api_started", env=settings.env)
    try:
        yield
    finally:
        if tailer is not None:
            await tailer.stop()
        if shipper is not None:
            set_log_shipper(None)
            await shipper.stop()
        await container.shutdown()
        _logger.info("api_stopped")


async def _start_log_bridge(
    app: FastAPI, container: Container, settings: Settings
) -> tuple[LogShipper | None, LogTailer | None]:
    """Start log shipping + tailing, or return ``(None, None)`` if disabled.

    Never fatal: if Redis is unreachable the terminal simply falls back to the
    API's own lines, which is exactly the behaviour before this existed.
    """
    if not settings.observability.log_shipping_enabled:
        return None, None
    shipper = LogShipper(container.redis, role="api")
    await shipper.start()
    set_log_shipper(shipper)
    tailer = LogTailer(container.redis, own_role="api")
    await tailer.start(get_log_buffer())
    app.state.log_tailer = tailer
    return shipper, tailer


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Hades",
        version=__version__,
        description="Professional quantitative platform for Solana meme coins.",
        docs_url="/docs" if settings.api.enable_docs else None,
        redoc_url="/redoc" if settings.api.enable_docs else None,
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(metrics.router)
    app.include_router(version.router)
    app.include_router(info.router)
    app.include_router(status.router)
    app.include_router(config.router)
    app.include_router(trading.router)
    app.include_router(execution.router)
    app.include_router(scanner.router)
    app.include_router(security.router)
    app.include_router(intelligence.router)
    app.include_router(committee.router)
    app.include_router(strategy.router)
    app.include_router(portfolio.router)
    app.include_router(risk.router)
    app.include_router(funnel.router)
    app.include_router(research.router)
    app.include_router(knowledge.router)
    app.include_router(audit.router)
    app.include_router(ws_routes.router)

    return app
