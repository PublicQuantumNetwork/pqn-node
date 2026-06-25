from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Depends
from pqn_hardware.drivers.rotaryencoder import MockRotaryEncoder
from pqn_hardware.drivers.rotaryencoder import RotaryEncoderInstrument
from pqn_hardware.drivers.rotaryencoder import SerialRotaryEncoder
from pqn_hardware.network.client import Client

from pqn_node.core.config import NodeState
from pqn_node.core.config import get_state
from pqn_node.core.config import logger
from pqn_node.core.config import settings


async def get_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(timeout=60) as client:
        yield client


ClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


async def get_instrument_client() -> AsyncGenerator[Client, None]:
    async with Client(host=settings.router_address, port=settings.router_port, router_name=settings.router_name) as client:
        yield client


InstrumentClientDep = Annotated[httpx.AsyncClient, Depends(get_instrument_client)]


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
