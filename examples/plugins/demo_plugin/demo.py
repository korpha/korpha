"""Demo plugin entry point. Registered via plugin.yaml's `entry_point`."""
from __future__ import annotations

import logging

from korpha.heartbeats.dispatcher import HandlerContext
from korpha.plugins import PluginHost

logger = logging.getLogger(__name__)


async def _on_demo_tick(ctx: HandlerContext) -> None:
    logger.info(
        "demo plugin tick: business=%s payload=%s",
        ctx.wakeup.business_id,
        ctx.wakeup.payload,
    )


def register(host: PluginHost) -> None:
    """Called once at plugin load. Use the host to declare contributions."""
    host.add_wakeup_handler("demo.tick", _on_demo_tick)
