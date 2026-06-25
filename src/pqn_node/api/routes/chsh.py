import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from typing import cast

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from fastapi.responses import StreamingResponse
from pqn_hardware.network.client import Client
from pydantic import BaseModel

from pqn_node.api.deps import ClientDep
from pqn_node.api.deps import StateDep
from pqn_node.core.config import chsh_progress_event
from pqn_node.core.config import settings

if TYPE_CHECKING:
    from pqn_hardware.instrument import RotatorInstrument

logger = logging.getLogger(__name__)


class ChshResult(BaseModel):
    chsh_value: float
    chsh_error: float
    expectation_values: list[float]
    expectation_errors: list[float]
    expectation_values_sign_fixed: list[float]


router = APIRouter(prefix="/chsh", tags=["chsh"])


@router.get("/progress")
async def chsh_progress(state: StateDep) -> StreamingResponse:
    """SSE endpoint for streaming CHSH measurement progress to frontend."""

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Send initial connection event
            yield f"data: {json.dumps({'event': 'connected'})}\n\n"

            while True:
                event_sent = False

                # Check for progress event
                try:
                    await asyncio.wait_for(chsh_progress_event.wait(), timeout=1.0)
                    yield f"data: {json.dumps({'event': 'chsh_progress', 'current': state.chsh_progress_current, 'total': state.chsh_progress_total, 'running': state.chsh_running})}\n\n"
                    chsh_progress_event.clear()
                    event_sent = True
                except TimeoutError:
                    pass

                # Send heartbeat if no event was sent to keep connection alive
                if not event_sent:
                    yield ":\n"

                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info("CHSH SSE connection closed by client")
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _chsh(  # Complexity is high due to the nature of the CHSH experiment.
    basis: tuple[float, float],
    http_client: ClientDep,
    timetagger_address: str,
    state: StateDep,
) -> ChshResult:
    follower_node_address = settings.follower_node_address
    if follower_node_address is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="follower_node_address not configured")
    logger.debug("Starting CHSH")

    # Initialize progress tracking
    state.chsh_running = True
    state.chsh_progress_current = 0
    state.chsh_progress_total = 16  # 2 basis x 2 follower x 2 angles x 2 perp
    chsh_progress_event.set()

    logger.debug("Instantiating client")
    client = Client(host=settings.router_address, port=settings.router_port, timeout=600_000)

    # TODO: Check if settings.chsh_settings.hwp is set before even trying to get the device.
    hwp = cast("RotatorInstrument", client.get_device(settings.chsh_settings.hwp[0], settings.chsh_settings.hwp[1]))
    if hwp is None:
        logger.error("Could not find half waveplate device")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find half waveplate device",
        )

    logger.debug("Halfwaveplate device found: %s", hwp)

    expectation_values = []
    expectation_errors = []
    basis = (0, abs(basis[0] - basis[1]) % 90)
    for angle in basis:  # Going through my basis angles
        for i in range(2):  # Going through follower basis angles
            counts = []
            for a in [angle, (angle + 90)]:
                hwp.move_to(a / 2)
                for perp in [False, True]:
                    r = await http_client.post(
                        f"http://{follower_node_address}/chsh/request-angle-by-basis?index={i}&perp={perp}"
                    )
                    # TODO: Handle other status codes
                    if r.status_code != status.HTTP_200_OK:
                        logger.error("Failed to request follower: %s", r.text)
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to request follower",
                        )

                    count_ret = await http_client.get(
                        f"http://{timetagger_address}/timetagger/measure_correlation?integration_time_s={settings.chsh_settings.measurement_config.integration_time_s}&coincidence_window_ps={settings.chsh_settings.measurement_config.binwidth_ps}&channel1={settings.chsh_settings.measurement_config.channel1}&channel2={settings.chsh_settings.measurement_config.channel2}&dark_count={settings.chsh_settings.measurement_config.dark_count}"
                    )
                    if count_ret.status_code != status.HTTP_200_OK:
                        logger.error("Failed to get correlation from timetagger: %s", count_ret.text)
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to get correlation from timetagger",
                        )
                    count = cast("int", count_ret.json())
                    counts.append(count)

                    # Update progress
                    state.chsh_progress_current += 1
                    chsh_progress_event.set()

            # Calculating expectation value
            numerator = counts[0] - counts[1] - counts[2] + counts[3]
            denominator = sum(counts) - 4 * settings.chsh_settings.measurement_config.dark_count
            expectation_value = 0 if denominator == 0 else numerator / denominator
            expectation_values.append(expectation_value)

            # Calculating error
            error = calculate_chsh_expectation_error(counts, settings.chsh_settings.measurement_config.dark_count)
            expectation_errors.append(error)

            logger.info(
                "For angle %s, for follower index %s, expectation value: %s, error: %s",
                angle,
                i,
                expectation_value,
                error,
            )

    logger.info("Expectation values: %s", expectation_values)
    logger.info("Expectation errors: %s", expectation_errors)

    # FIXME: This is a temporary fix for handling impossible expectation values. We should not have to rely on the settings for this.
    expectation_values_sign_fixed = [
        x * y for x, y in zip(expectation_values, settings.chsh_settings.expectation_signs, strict=False)
    ]

    logger.info("After passing signed calculation: %s", expectation_values_sign_fixed)
    chsh_value = abs(sum(x for x in expectation_values_sign_fixed))
    chsh_error = sum(x**2 for x in expectation_errors) ** 0.5

    # Mark CHSH as complete
    state.chsh_running = False
    chsh_progress_event.set()

    return ChshResult(
        chsh_value=chsh_value,
        chsh_error=chsh_error,
        expectation_values=expectation_values,
        expectation_errors=expectation_errors,
        expectation_values_sign_fixed=expectation_values_sign_fixed,
    )


@router.post("/")
async def chsh(
    basis: tuple[float, float],
    http_client: ClientDep,
    timetagger_address: str,
    state: StateDep,
) -> ChshResult:
    logger.info("Starting CHSH experiment with basis: %s", basis)
    return await _chsh(basis, http_client, timetagger_address, state)


@router.post("/request-angle-by-basis")
async def request_angle_by_basis(index: int, state: StateDep, *, perp: bool = False) -> bool:
    client = Client(host=settings.router_address, port=settings.router_port, timeout=600_000)
    hwp = cast(
        "RotatorInstrument",
        client.get_device(settings.chsh_settings.request_hwp[0], settings.chsh_settings.request_hwp[1]),
    )
    if hwp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find half waveplate device",
        )

    angle = state.chsh_request_basis[index] + 90 * perp
    hwp.move_to(angle / 2)
    logger.info("moving waveplate", extra={"angle": angle})
    return True


def calculate_chsh_expectation_error(counts: list[int], dark_count: int = 0) -> float:
    total_counts = sum(counts)
    corrected_total = total_counts - 4 * dark_count
    if corrected_total <= 0:
        return 0
    first_term = (total_counts**0.5) / corrected_total
    expectation = abs(counts[0] + counts[3] - counts[1] - counts[2])
    second_term = (expectation / corrected_total**2) * (total_counts + 4 * dark_count) ** 0.5
    return float(first_term + second_term)
