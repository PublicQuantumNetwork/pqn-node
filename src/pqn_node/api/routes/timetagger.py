import logging
from typing import TYPE_CHECKING
from typing import Annotated
from typing import cast

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pqn_hardware.measurement import MeasurementConfig
from pqn_hardware.network.client import Client

from pqn_node.core.config import settings

if TYPE_CHECKING:
    from pqn_hardware.instrument import TimeTaggerInstrument

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/timetagger", tags=["timetagger"])


@router.get("/measure_correlation")
async def measure_correlation(
    integration_time_s: float,
    coincidence_window_ps: int = 500,
    channel1: int = 1,
    channel2: int = 2,
) -> int:
    if settings.timetagger is None:
        logger.error("No timetagger configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No timetagger configured",
        )

    mconf = MeasurementConfig(
        integration_time_s=integration_time_s,
        binwidth_ps=coincidence_window_ps,
        channel1=channel1,
        channel2=channel2,
    )
    client = Client(host=settings.router_address, port=settings.router_port, router_name=settings.router_name, timeout=600_000)
    tagger = cast("TimeTaggerInstrument", client.get_device(settings.timetagger[0], settings.timetagger[1]))
    if tagger is None:
        logger.error("Could not find time tagger device")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find time tagger device",
        )

    logger.debug("Time tagger device found: %s", tagger)
    count = tagger.measure_correlation(
        mconf.channel1,
        mconf.channel2,
        integration_time_s=mconf.integration_time_s,
        binwidth_ps=mconf.binwidth_ps,
    )

    logger.info("Measured %d coincidences", count)
    return int(count)


@router.get("/count_singles")
async def count_singles(
    integration_time_s: float,
    channels: Annotated[list[int], Query()],
) -> list[int]:
    if settings.timetagger is None:
        logger.error("No timetagger configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No timetagger configured",
        )

    client = Client(host=settings.router_address, port=settings.router_port, router_name=settings.router_name, timeout=600_000)
    tagger = cast("TimeTaggerInstrument", client.get_device(settings.timetagger[0], settings.timetagger[1]))
    if tagger is None:
        logger.error("Could not find time tagger device")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find time tagger device",
        )

    logger.debug("Time tagger device found: %s", tagger)
    counts = tagger.count_singles(channels, integration_time_s=integration_time_s)

    logger.info("Measured singles counts: %s", counts)
    return [int(c) for c in counts]
