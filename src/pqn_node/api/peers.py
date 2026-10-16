"""Calls a Node makes to the other machines in a game: its follower and the timetagger."""

import logging
from typing import cast

import httpx
from fastapi import HTTPException
from fastapi import status
from pqn_hardware.measurement import MeasurementConfig

from pqn_node.core.config import settings

logger = logging.getLogger(__name__)


def require_follower_address() -> str:
    """Return the configured follower's address, or answer 503 when this Node has none."""
    if settings.follower_node_address is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="follower_node_address not configured"
        )
    return settings.follower_node_address


async def fetch_node_name(http_client: httpx.AsyncClient, node_address: str) -> str:
    """Ask another Node what it is called, falling back to its address.

    Only ever a label, so a Node that will not say is no reason to fail whatever wanted it.
    """
    try:
        r = await http_client.get(f"http://{node_address}/node/config")
        name = r.json().get("node_name") if r.status_code == status.HTTP_200_OK else None
    except (httpx.HTTPError, ValueError, AttributeError):
        name = None
    return name or node_address


async def measure_coincidences(
    http_client: httpx.AsyncClient,
    timetagger_address: str,
    cfg: MeasurementConfig,
    integration_time_s: float | None = None,
) -> int:
    """Count coincidences on the timetagger, integrating for ``cfg``'s time unless told otherwise."""
    r = await http_client.get(
        f"http://{timetagger_address}/timetagger/measure_correlation",
        params={
            "integration_time_s": cfg.integration_time_s if integration_time_s is None else integration_time_s,
            "coincidence_window_ps": cfg.binwidth_ps,
            "channel1": cfg.channel1,
            "channel2": cfg.channel2,
        },
    )
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to get correlation from timetagger: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get correlation from timetagger",
        )
    return cast("int", r.json())
