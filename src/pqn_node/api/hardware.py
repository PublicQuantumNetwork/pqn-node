"""Finding and moving this Node's instruments through the router.

Plain helpers called from inside routes, not FastAPI dependencies: which device to find
depends on the route, and a missing one should answer with that route's error.
"""

import logging
from typing import TYPE_CHECKING
from typing import cast

from fastapi import HTTPException
from fastapi import status
from pqn_hardware.network.client import Client

from pqn_node.core.config import settings

if TYPE_CHECKING:
    from pqn_hardware.instrument import RotatorInstrument
    from pqn_hardware.instrument import TimeTaggerInstrument

logger = logging.getLogger(__name__)

INSTRUMENT_TIMEOUT_MS = 600_000
"""How long a call to an instrument may take. Generous because a waveplate move or a long integration is slow."""


def router_client(timeout_ms: int = 30_000) -> Client:
    """Open a client to this Node's router, as configured."""
    return Client(
        host=settings.router_address, port=settings.router_port, router_name=settings.router_name, timeout=timeout_ms
    )


def find_rotator(device: tuple[str, str]) -> "RotatorInstrument":
    """Find a half waveplate on the router, or answer 404 if the router does not have it."""
    hwp = cast("RotatorInstrument | None", router_client(INSTRUMENT_TIMEOUT_MS).get_device(device[0], device[1]))
    if hwp is None:
        logger.error("Could not find half waveplate device %s", device)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not find half waveplate device")
    logger.debug("Halfwaveplate device found: %s", hwp)
    return hwp


def find_timetagger() -> "TimeTaggerInstrument":
    """Find this Node's time tagger on the router: 500 if none is configured, 404 if the router lacks it."""
    if settings.timetagger is None:
        logger.error("No timetagger configured")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No timetagger configured")
    tagger = cast(
        "TimeTaggerInstrument | None",
        router_client(INSTRUMENT_TIMEOUT_MS).get_device(settings.timetagger[0], settings.timetagger[1]),
    )
    if tagger is None:
        logger.error("Could not find time tagger device")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not find time tagger device")
    logger.debug("Time tagger device found: %s", tagger)
    return tagger


def move_to_polarization(hwp: "RotatorInstrument", polarization_deg: float) -> float:
    """Turn a half waveplate so it sets this polarization angle, which takes half as much turning.

    Returns the angle the waveplate was sent to, so a caller reports what was commanded rather
    than working it out again.
    """
    waveplate_deg = polarization_deg / 2
    hwp.move_to(waveplate_deg)
    return waveplate_deg
