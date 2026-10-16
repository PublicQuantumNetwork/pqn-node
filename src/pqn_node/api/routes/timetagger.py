import logging
from typing import Annotated

from fastapi import APIRouter
from fastapi import Query
from pqn_hardware.measurement import MeasurementConfig

from pqn_node.api.hardware import find_timetagger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/timetagger", tags=["timetagger"])


@router.get("/measure_correlation")
async def measure_correlation(
    integration_time_s: float,
    coincidence_window_ps: int = 500,
    channel1: int = 1,
    channel2: int = 2,
) -> int:
    mconf = MeasurementConfig(
        integration_time_s=integration_time_s,
        binwidth_ps=coincidence_window_ps,
        channel1=channel1,
        channel2=channel2,
    )
    tagger = find_timetagger()
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
    tagger = find_timetagger()
    counts = tagger.count_singles(channels, integration_time_s=integration_time_s)

    logger.info("Measured singles counts: %s", counts)
    return [int(c) for c in counts]
