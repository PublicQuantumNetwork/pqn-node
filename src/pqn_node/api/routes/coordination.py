import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi import status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pqn_node.api.deps import ClientDep
from pqn_node.api.deps import StateDep
from pqn_node.core.config import NodeRole
from pqn_node.core.config import ask_user_for_follow_event
from pqn_node.core.config import protocol_cancelled_event
from pqn_node.core.config import settings
from pqn_node.core.config import user_replied_event

logger = logging.getLogger(__name__)


class FollowRequestResponse(BaseModel):
    accepted: bool


class CollectFollowerResponse(BaseModel):
    accepted: bool


class ResetCoordinationStateResponse(BaseModel):
    message: str = "Coordination state reset successfully"


class ProtocolCancellationNotification(BaseModel):
    reason: str = "Protocol cancelled by peer"
    cancelled_by_role: str


router = APIRouter(prefix="/coordination", tags=["coordination"])


# TODO: Send a disconnection message if I was following/leading someone.
# FIXME: This is technically resetting more than just coordination state. including qkd.
@router.post("/reset_coordination_state")
async def reset_coordination_state(state: StateDep, http_client: ClientDep) -> ResetCoordinationStateResponse:
    """Reset the coordination state of the node."""
    # Notify peer node BEFORE resetting state
    peer_address = None
    current_role = state.role

    if state.role == NodeRole.LEADER and state.followers_address:
        peer_address = state.followers_address
    elif state.role == NodeRole.FOLLOWER and state.leaders_address:
        peer_address = state.leaders_address

    # Try to notify peer (best-effort, don't fail if peer is unreachable)
    if peer_address:
        try:
            logger.info("Notifying peer at %s about protocol cancellation", peer_address)
            await http_client.post(
                f"http://{peer_address}/coordination/protocol_cancelled",
                json={"reason": "Protocol cancelled by user", "cancelled_by_role": current_role.value},
                timeout=5.0,  # Short timeout to avoid hanging
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to notify peer about cancellation: %s. Proceeding with reset.", str(e))

    # Set local cancellation event to unblock any waiting operations
    protocol_cancelled_event.set()

    # Reset state
    state.role = NodeRole.INDEPENDENT
    state.followers_address = ""
    state.following_requested = False
    state.following_requested_user_response = None
    state.leaders_address = ""
    state.leaders_name = ""
    state.qkd_emoji_pick = ""
    state.qkd_bit_list = []
    state.qkd_question_order = []
    state.qkd_leader_basis_list = []
    state.qkd_follower_basis_list = []
    state.qkd_single_bit_current_index = 0
    state.qkd_resulting_bit_list = []
    state.qkd_request_basis_list = []
    state.qkd_request_bit_list = []
    state.qkd_n_matching_bits = -1

    # Clear the cancellation event for next use
    protocol_cancelled_event.clear()

    return ResetCoordinationStateResponse()


@router.post("/protocol_cancelled")
async def protocol_cancelled(
    notification: ProtocolCancellationNotification,
) -> dict[str, str]:
    """Receive notification that peer node cancelled the protocol."""
    logger.info("Received protocol cancellation from %s: %s", notification.cancelled_by_role, notification.reason)
    protocol_cancelled_event.set()

    # Give waiting operations a chance to wake up and handle the cancellation
    # Then clear for the next operation
    await asyncio.sleep(0.5)
    protocol_cancelled_event.clear()

    return {"status": "acknowledged"}


@router.post("/collect_follower")
async def collect_follower(
    request: Request, state: StateDep, http_client: ClientDep
) -> CollectFollowerResponse:
    """
    Endpoint called by a leader node (this one) to request a follower node (other node) to follow it.

    Returns
    -------
        CollectFollowerResponse indicating if the follower accepted the request.
    """
    address = settings.follower_node_address
    if address is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="follower_node_address not configured")
    logger.info("Requesting client at %s to follow", address)

    # Get the port this server is listening on
    server_port = request.scope["server"][1]

    ret = await http_client.post(
        f"http://{address}/coordination/follow_requested?leaders_name={settings.node_name}&leaders_port={server_port}"
    )
    if ret.status_code != status.HTTP_200_OK:
        raise HTTPException(status_code=ret.status_code, detail=ret.text)

    response_data = FollowRequestResponse(**ret.json())
    if response_data.accepted:
        state.role = NodeRole.LEADER
        state.followers_address = address
        logger.info("Successfully collected follower")
        return CollectFollowerResponse(accepted=True)

    logger.info("Follower rejected follow request")
    return CollectFollowerResponse(accepted=False)


@router.post("/follow_requested")
async def follow_requested(
    request: Request, leaders_name: str, leaders_port: int, state: StateDep
) -> FollowRequestResponse:
    """
    Endpoint is called by a leader node (other node) to request this node to follow it.

    Returns
    -------
        FollowRequestResponse indicating if the follow request is accepted.
    """
    logger.debug("Requesting client at %s to follow", leaders_name)

    if request.client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request lacks the clients host")
    leaders_address = f"{request.client.host}:{leaders_port}"

    # Check if the client is ready to accept a follower request and that node is not already following someone.
    if not state.client_listening_for_follower_requests or state.role != NodeRole.INDEPENDENT:
        logger.info(
            "Request rejected because %s",
            (
                "client is not listening for requests"
                if not state.client_listening_for_follower_requests
                else f"this node is already a {state.role}"
            ),
        )
        return FollowRequestResponse(accepted=False)

    state.following_requested = True
    state.leaders_name = leaders_name
    state.leaders_address = leaders_address
    # Trigger the state change to get the websocket to send question to user
    ask_user_for_follow_event.set()

    logger.debug("Asking user to accept follow request from %s (%s)", leaders_name, leaders_address)

    # Wait for EITHER user reply OR cancellation
    _done, pending = await asyncio.wait(
        [asyncio.create_task(user_replied_event.wait()), asyncio.create_task(protocol_cancelled_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel pending tasks
    for task in pending:
        task.cancel()

    # Check if protocol was cancelled
    if protocol_cancelled_event.is_set():
        logger.warning("Follow request cancelled")
        # Clean up state
        state.leaders_address = ""
        state.leaders_name = ""
        state.following_requested = False
        state.following_requested_user_response = None
        return FollowRequestResponse(accepted=False)

    user_replied_event.clear()  # Reset the event for the next change
    if state.following_requested_user_response:
        logger.debug("Follow request from %s accepted.", leaders_address)
        state.role = NodeRole.FOLLOWER
        state.leaders_name = leaders_name
        state.leaders_address = leaders_address
        return FollowRequestResponse(accepted=True)

    logger.debug("Follow request from %s rejected.", leaders_address)
    # Clean up the state if user rejected
    state.leaders_address = ""
    state.leaders_name = ""
    state.following_requested = False
    state.following_requested_user_response = None
    return FollowRequestResponse(accepted=False)


@router.websocket("/follow_requested_alerts")
async def follow_requested_alert(websocket: WebSocket, state: StateDep) -> None:
    """Websocket endpoint is used to alert the client when a follow request is received. It also handles the response from the client."""
    await websocket.accept()
    logger.info("Client connected to websocket for multiplayer coordination.")
    state.client_listening_for_follower_requests = True

    async def ask_user_for_follow_handler() -> None:
        """Task that waits for the ask_user_for_follow_event event and sends a message to the client if a follow request is detected."""
        while True:
            try:
                await ask_user_for_follow_event.wait()  # Wait for a state change event
                if state.following_requested:
                    logger.debug("Websocket detected a follow request, asking user for response.")
                    if websocket.client_state.name == "CONNECTED":
                        await websocket.send_text(f"Do you want to accept a connection from {state.leaders_name}?")
                    else:
                        logger.debug("WebSocket not connected, cannot send message")
                        break
                ask_user_for_follow_event.clear()  # Reset the event for the next change
            except WebSocketDisconnect:
                logger.info("WebSocket disconnected in ask_user_for_follow_handler")
                break
            except Exception:
                logger.exception("Error in ask_user_for_follow_handler, continuing to listen")
                ask_user_for_follow_event.clear()  # Reset the event to continue

    async def client_message_handler() -> None:
        """Task that waits for a message from the client and handles the response. It also handles the case where the client disconnects."""
        try:
            while True:
                response = await websocket.receive_text()
                state.following_requested_user_response = response.lower() in ["true", "yes", "y"]
                state.following_requested = False
                logger.debug("Websocket received a response from user: %s", state.following_requested_user_response)
                user_replied_event.set()
        except WebSocketDisconnect:
            logger.info("Client disconnected from websocket for multiplayer coordination.")
            state.client_listening_for_follower_requests = False

    state_change_task = asyncio.create_task(ask_user_for_follow_handler())
    client_message_task = asyncio.create_task(client_message_handler())

    try:
        await asyncio.gather(state_change_task, client_message_task)
    finally:
        state_change_task.cancel()
        client_message_task.cancel()


@router.get("/state_events")
async def state_events(state: StateDep) -> StreamingResponse:
    """SSE endpoint for streaming state change events to frontend."""

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Send initial connection event
            yield f"data: {json.dumps({'event': 'connected', 'role': state.role.value})}\n\n"

            while True:
                event_sent = False

                # Check for cancellation event
                try:
                    await asyncio.wait_for(protocol_cancelled_event.wait(), timeout=1.0)
                    yield f"data: {json.dumps({'event': 'protocol_cancelled', 'reason': 'Protocol cancelled by peer or user'})}\n\n"
                    protocol_cancelled_event.clear()
                    event_sent = True
                except TimeoutError:
                    pass

                # Send heartbeat if no event was sent to keep connection alive
                if not event_sent:
                    yield ":\n"

                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info("SSE connection closed by client")
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
