from fastapi import APIRouter
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
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import settings
from pqn_node.core.config import update_config


class NodeConfig(BaseModel):
    node_name: str
    follower_node_address: str | None


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
