from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import TYPE_CHECKING
from typing import Annotated
from typing import cast

import httpx
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pqn_hardware.drivers.rotaryencoder import MockRotaryEncoder
from pqn_hardware.drivers.rotaryencoder import RotaryEncoderInstrument
from pqn_hardware.drivers.rotaryencoder import SerialRotaryEncoder
from pqn_hardware.network.client import Client

from pqn_node.core.config import NodeState
from pqn_node.core.config import get_state
from pqn_node.core.config import logger
from pqn_node.core.config import settings

if TYPE_CHECKING:
    from pqn_hardware.instrument import RotatorInstrument
    from pqn_hardware.instrument import TimeTaggerInstrument


async def get_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(timeout=60) as client:
        yield client


ClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


async def get_instrument_client() -> AsyncGenerator[Client, None]:
    async with Client(
        host=settings.router_address, port=settings.router_port, router_name=settings.router_name
    ) as client:
        yield client


InstrumentClientDep = Annotated[httpx.AsyncClient, Depends(get_instrument_client)]


def _router_client() -> Client:
    return Client(
        host=settings.router_address, port=settings.router_port, router_name=settings.router_name, timeout=600_000
    )


def get_rotator(device: tuple[str, str]) -> "RotatorInstrument":
    """Find a half waveplate on the router, or answer 404 if the router does not have it."""
    hwp = cast("RotatorInstrument | None", _router_client().get_device(device[0], device[1]))
    if hwp is None:
        logger.error("Could not find half waveplate device %s", device)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not find half waveplate device")
    logger.debug("Halfwaveplate device found: %s", hwp)
    return hwp


def get_timetagger() -> "TimeTaggerInstrument":
    """Find this Node's time tagger on the router: 500 if none is configured, 404 if the router lacks it."""
    if settings.timetagger is None:
        logger.error("No timetagger configured")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No timetagger configured")
    tagger = cast(
        "TimeTaggerInstrument | None", _router_client().get_device(settings.timetagger[0], settings.timetagger[1])
    )
    if tagger is None:
        logger.error("Could not find time tagger device")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not find time tagger device")
    logger.debug("Time tagger device found: %s", tagger)
    return tagger


def move_to_polarization(hwp: "RotatorInstrument", polarization_deg: float) -> None:
    """Turn a half waveplate so it sets this polarization angle, which takes half as much turning."""
    hwp.move_to(polarization_deg / 2)


StateDep = Annotated[NodeState, Depends(get_state)]


@lru_cache
def get_rotary_encoder() -> RotaryEncoderInstrument:
    if settings.virtual_rotator:
        # Virtual rotator mode enabled, use mock with terminal input
        logger.info("Virtual rotator mode enabled")
        rotary_encoder: RotaryEncoderInstrument = MockRotaryEncoder()
    else:
        # Use the real serial rotary encoder
        rotary_encoder = SerialRotaryEncoder(
            label="rotary_encoder", address=settings.rotary_encoder_address, offset_degrees=0.0
        )

    return rotary_encoder


SERDep = Annotated[RotaryEncoderInstrument, Depends(get_rotary_encoder)]
