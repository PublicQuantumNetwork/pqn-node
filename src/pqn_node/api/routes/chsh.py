import asyncio
import json
import logging
import math
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import ValidationError

from pqn_node.api.deps import ClientDep
from pqn_node.api.deps import StateDep
from pqn_node.api.hardware import find_rotator
from pqn_node.api.hardware import move_to_polarization
from pqn_node.api.peers import fetch_node_name
from pqn_node.api.peers import measure_coincidences
from pqn_node.api.peers import require_follower_address
from pqn_node.core.config import chsh_progress_event
from pqn_node.core.config import settings

logger = logging.getLogger(__name__)


class WaveplateMove(BaseModel):
    """Where a Node sent its waveplate: the polarization angle, and the waveplate angle that set it."""

    polarization: float
    waveplate: float


class ChshMeasurement(BaseModel):
    """One of a CHSH run's sixteen settings: where both waveplates were sent, and what was counted.

    Polarization angles, and the waveplate angles that set them. These are the angles commanded,
    not read back from the motors. The follower's are ``None`` when it does not report them,
    which a follower from before this field does not.
    """

    leader_polarization: float
    leader_waveplate: float
    follower_polarization: float | None
    follower_waveplate: float | None
    coincidences: int


class ChshResult(BaseModel):
    chsh_value: float
    chsh_error: float
    expectation_values: list[float]
    expectation_errors: list[float]
    expectation_values_sign_fixed: list[float]
    measurements: list[ChshMeasurement] = []


class SweepResult(BaseModel):
    """An Angle Sweep: the leader's polarization angles and the coincidences counted at each.

    Angles are polarization angles, the ones a plot's x axis shows; the half waveplate is
    moved to half of each, which ``move_to_polarization`` takes care of.
    """

    leader_name: str
    follower_name: str
    follower_angle: float
    step_deg: float
    integration_time_s: float
    angles: list[float]
    coincidences: list[int]

    @property
    def min_coincidences(self) -> int:
        return min(self.coincidences, default=0)

    @property
    def max_coincidences(self) -> int:
        return max(self.coincidences, default=0)

    @property
    def visibility(self) -> float:
        """Fringe visibility, (max - min) / (max + min). Zero when nothing was counted."""
        low, high = self.min_coincidences, self.max_coincidences
        return 0.0 if high + low == 0 else (high - low) / (high + low)


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
    follower_node_address = require_follower_address()
    logger.debug("Starting CHSH")

    # Initialize progress tracking
    state.chsh_running = True
    state.chsh_progress_current = 0
    state.chsh_progress_total = 16  # 2 basis x 2 follower x 2 angles x 2 perp
    chsh_progress_event.set()

    # TODO: Check if settings.chsh_settings.hwp is set before even trying to get the device.
    hwp = find_rotator(settings.chsh_settings.hwp)

    expectation_values = []
    expectation_errors = []
    measurements: list[ChshMeasurement] = []
    basis = (0, abs(basis[0] - basis[1]) % 90)
    for angle in basis:  # Going through my basis angles
        for i in range(2):  # Going through follower basis angles
            counts = []
            for a in [angle, (angle + 90)]:
                leader_waveplate = move_to_polarization(hwp, a)
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

                    counts.append(
                        await measure_coincidences(
                            http_client, timetagger_address, settings.chsh_settings.measurement_config
                        )
                    )
                    follower = _reported_move(r.json())
                    measurements.append(
                        ChshMeasurement(
                            leader_polarization=a,
                            leader_waveplate=leader_waveplate,
                            follower_polarization=None if follower is None else follower.polarization,
                            follower_waveplate=None if follower is None else follower.waveplate,
                            coincidences=counts[-1],
                        )
                    )

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
        measurements=measurements,
    )


def _reported_move(body: object) -> WaveplateMove | None:
    """Read where a follower says it sent its waveplate, or ``None`` if it did not say.

    A follower from before it reported its move answers ``true``.
    """
    try:
        return WaveplateMove.model_validate(body)
    except ValidationError:
        return None


@router.post("/")
async def chsh(
    basis: tuple[float, float],
    http_client: ClientDep,
    timetagger_address: str,
    state: StateDep,
) -> ChshResult:
    logger.info("Starting CHSH experiment with basis: %s", basis)
    return await _chsh(basis, http_client, timetagger_address, state)


def _move_request_hwp(angle: float) -> WaveplateMove:
    """Move this Node's requested waveplate, the one a leader steers, to a polarization angle.

    Returns where it went, so the leader can report this Node's waveplate without assuming
    how this Node turns a polarization into a waveplate angle.
    """
    waveplate = move_to_polarization(find_rotator(settings.chsh_settings.request_hwp), angle)
    logger.info("moving waveplate", extra={"angle": angle, "waveplate": waveplate})
    return WaveplateMove(polarization=angle, waveplate=waveplate)


@router.post("/request-angle-by-basis")
async def request_angle_by_basis(index: int, state: StateDep, *, perp: bool = False) -> WaveplateMove:
    return _move_request_hwp(state.chsh_request_basis[index] + 90 * perp)


@router.post("/request-angle")
async def request_angle(angle: float) -> WaveplateMove:
    """Move the requested waveplate to a free polarization angle, rather than one of the basis angles."""
    return _move_request_hwp(angle)


@router.post("/sweep")
async def sweep(
    http_client: ClientDep,
    timetagger_address: str,
    follower_angle: float,
    step_deg: Annotated[float, Query(gt=0, le=360)] = 10.0,
    integration_time_s: Annotated[float, Query(gt=0)] = 1.0,
) -> SweepResult:
    """Pin the follower's waveplate at one angle and sweep ours through 0-360 degrees of polarization.

    Returns the coincidence count at every step, for plotting a fringe.
    """
    follower_node_address = require_follower_address()
    hwp = find_rotator(settings.chsh_settings.hwp)

    r = await http_client.post(f"http://{follower_node_address}/chsh/request-angle", params={"angle": follower_angle})
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to request follower: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to request follower",
        )

    follower_name = await fetch_node_name(http_client, follower_node_address)

    angles = [i * step_deg for i in range(math.ceil(360 / step_deg))]
    coincidences = []
    for angle in angles:
        move_to_polarization(hwp, angle)
        coincidences.append(
            await measure_coincidences(
                http_client, timetagger_address, settings.chsh_settings.measurement_config, integration_time_s
            )
        )
        logger.info("Sweep angle %s: %s coincidences", angle, coincidences[-1])

    return SweepResult(
        leader_name=settings.node_name,
        follower_name=follower_name,
        follower_angle=follower_angle,
        step_deg=step_deg,
        integration_time_s=integration_time_s,
        angles=angles,
        coincidences=coincidences,
    )


def calculate_chsh_expectation_error(counts: list[int], dark_count: int = 0) -> float:
    total_counts = sum(counts)
    corrected_total = total_counts - 4 * dark_count
    if corrected_total <= 0:
        return 0
    first_term = (total_counts**0.5) / corrected_total
    expectation = abs(counts[0] + counts[3] - counts[1] - counts[2])
    second_term = (expectation / corrected_total**2) * (total_counts + 4 * dark_count) ** 0.5
    return float(first_term + second_term)
