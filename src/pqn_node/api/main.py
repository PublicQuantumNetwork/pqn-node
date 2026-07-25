from fastapi import APIRouter
from pydantic import BaseModel

from pqn_node.api.routes import chsh
from pqn_node.api.routes import coordination
from pqn_node.api.routes import debug
from pqn_node.api.routes import health
from pqn_node.api.routes import qkd
from pqn_node.api.routes import rng
from pqn_node.api.routes import serial
from pqn_node.api.routes import timetagger
from pqn_node.api.routes.health import get_effective_availability
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import settings


class NodeConfig(BaseModel):
    follower_node_address: str | None


api_router = APIRouter()
api_router.include_router(chsh.router)
api_router.include_router(qkd.router)
api_router.include_router(timetagger.router)
api_router.include_router(rng.router)
api_router.include_router(serial.router)
api_router.include_router(coordination.router)
api_router.include_router(debug.router)
api_router.include_router(health.router)


@api_router.get("/games/availability", tags=["games"])
def get_availability() -> GamesAvailability:
    return get_effective_availability()


@api_router.get("/node/config", tags=["node"])
def get_node_config() -> NodeConfig:
    return NodeConfig(follower_node_address=settings.follower_node_address)
