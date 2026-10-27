from typing import Annotated

from fastapi import APIRouter
from fastapi import Body
from pydantic import BaseModel

from pqn_node.api.routes import chsh
from pqn_node.api.routes import coordination
from pqn_node.api.routes import health
from pqn_node.api.routes import qkd
from pqn_node.api.routes import rng
from pqn_node.api.routes import serial
from pqn_node.api.routes import system
from pqn_node.api.routes import timetagger
from pqn_node.api.routes.health import get_effective_availability
from pqn_node.constants import OperationMode
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import PolarizationAngles
from pqn_node.core.config import ReferenceRates
from pqn_node.core.config import settings
from pqn_node.core.config import update_config


class NodeConfig(BaseModel):
    node_name: str
    follower_node_address: str | None


class Calibration(BaseModel):
    """The Node's calibration: its Calibrated angles and its Reference rates."""

    polarization_angles: PolarizationAngles
    reference_rates: ReferenceRates


api_router = APIRouter()
api_router.include_router(chsh.router)
api_router.include_router(qkd.router)
api_router.include_router(timetagger.router)
api_router.include_router(rng.router)
api_router.include_router(serial.router)
api_router.include_router(coordination.router)
api_router.include_router(health.router)
api_router.include_router(system.router)


@api_router.get("/games/availability", tags=["games"])
def get_availability() -> GamesAvailability:
    return get_effective_availability()


@api_router.put("/games/availability", tags=["games"])
def set_availability(availability: GamesAvailability) -> GamesAvailability:
    """Set which games this Node offers, persistently and without a restart.

    Writes ``config.toml`` (comments preserved) *and* applies the change to the
    live settings object, so the response already reflects the new configuration
    gated by the most recent hardware probe. A game the hardware can't support
    stays unavailable no matter what is set here.
    """
    update_config({f"games_availability.{game}": value for game, value in availability.model_dump().items()})
    return get_effective_availability()


@api_router.get("/node/config", tags=["node"])
def get_node_config() -> NodeConfig:
    return NodeConfig(node_name=settings.node_name, follower_node_address=settings.follower_node_address)


@api_router.get("/node/operation_mode", tags=["node"])
def get_operation_mode() -> OperationMode:
    """Return the Operation mode this Node plays its two-player Games in, as that mode's own string."""
    return settings.operation_mode


@api_router.put("/node/operation_mode", tags=["node"])
def set_operation_mode(operation_mode: Annotated[OperationMode, Body()]) -> OperationMode:
    """Set the Operation mode this Node plays its two-player Games in, persistently and without a restart.

    Writes ``config.toml`` (comments preserved) *and* applies the change to the live
    settings object, so the response already reflects the new mode.
    """
    update_config({"operation_mode": operation_mode})
    return settings.operation_mode


@api_router.get("/node/calibration", tags=["node"])
def get_calibration() -> Calibration:
    """Return the Calibrated angles and the Reference rates this Node plays with right now."""
    return Calibration(
        polarization_angles=settings.polarization_angles,
        reference_rates=settings.correlation_settings.reference_rates,
    )


@api_router.put("/node/calibration", tags=["node"])
def set_calibration(calibration: Calibration) -> Calibration:
    """Set the Calibrated angles and the Reference rates, persistently and without a restart.

    Writes ``config.toml`` (comments preserved) *and* applies the change to the live
    settings object, so the next move to a named polarization already uses the new
    angles. The Reference rates land under ``correlation_settings``, where the
    correlation Operation mode reads them.
    """
    angles = calibration.polarization_angles.model_dump()
    rates = calibration.reference_rates.model_dump()
    update_config(
        {f"polarization_angles.{name}": angle for name, angle in angles.items()}
        | {f"correlation_settings.reference_rates.{name}": rate for name, rate in rates.items()}
    )
    return get_calibration()
