import asyncio
import logging
import random
import secrets
from typing import TYPE_CHECKING
from typing import cast

import httpx
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status
from pqn_hardware.network.client import Client
from pydantic import BaseModel

from pqn_node.api.deps import ClientDep
from pqn_node.api.deps import StateDep
from pqn_node.constants import BasisBool
from pqn_node.constants import QKDEncodingBasis
from pqn_node.core.config import NodeRole
from pqn_node.core.config import NodeState
from pqn_node.core.config import protocol_cancelled_event
from pqn_node.core.config import qkd_result_received_event
from pqn_node.core.config import settings

if TYPE_CHECKING:
    from pqn_hardware.instrument import RotatorInstrument

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/qkd", tags=["qkd"])


class QKDResult(BaseModel):
    n_matching_bits: int
    n_total_bits: int
    emoji: str
    role: str


async def _qkd(
    http_client: ClientDep,
    state: StateDep,
    timetagger_address: str | None = None,
    follower_node_address: str | None = None,
) -> list[int]:
    if follower_node_address is None:
        follower_node_address = settings.follower_node_address
    if follower_node_address is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="follower_node_address not configured")
    logger.debug("Starting QKD")
    client = Client(host=settings.router_address, port=settings.router_port, timeout=600_000)
    hwp = cast("RotatorInstrument", client.get_device(settings.qkd_settings.hwp[0], settings.qkd_settings.hwp[1]))

    if hwp is None:
        logger.error("Could not find half waveplate device")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find half waveplate device",
        )

    counts = []
    for basis in state.qkd_leader_basis_list:
        r = await http_client.post(f"http://{follower_node_address}/qkd/single_bit")

        if r.status_code != status.HTTP_200_OK:
            logger.error("Failed to handshake with follower: %s", r.text)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to handshake with follower",
            )
        logger.debug("Handshake with follower successful")

        int_choice = secrets.randbits(1)  # FIXME: Make this real quantum random.
        logger.debug("Chosen integer choice: %s", int_choice)
        state.qkd_bit_list.append(int_choice)
        hwp.move_to(basis.angles[int_choice].value)
        logger.debug("Moving half waveplate to angle: %s", basis.angles[int_choice].value)

        count_ret = await http_client.get(
            f"http://{timetagger_address}/timetagger/measure_correlation?integration_time_s={settings.chsh_settings.measurement_config.integration_time_s}&coincidence_window_ps={settings.chsh_settings.measurement_config.binwidth_ps}&channel1={settings.chsh_settings.measurement_config.channel1}&channel2={settings.chsh_settings.measurement_config.channel2}&dark_count={settings.chsh_settings.measurement_config.dark_count}"
        )
        if count_ret.status_code != status.HTTP_200_OK:
            logger.error("Failed to get correlation from timetagger: %s", count_ret.text)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to get correlation from timetagger",
            )

        c = cast("int", count_ret.json())
        counts.append(c)
        logger.debug("Counted %d coincidences", c)

    def get_outcome(state: int, basis: int, choice: int, counts: int) -> int:
        above = counts > settings.qkd_settings.discriminating_threshold
        return ((int(above) ^ choice) ^ (1 - state)) ^ basis

    outcome = []
    logger.debug(
        "Going for qkd_leader_basis_list: %s, qkd_bit_list: %s, counts: %s",
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        counts,
    )
    for basis, choice, count in zip(state.qkd_leader_basis_list, state.qkd_bit_list, counts, strict=False):
        out = get_outcome(settings.bell_state.value, BasisBool[basis.name].value, choice, count)
        logger.debug(
            "Calculating outcome for basis: %s, choice: %s, count: %s, outcome: %s", basis.name, choice, count, out
        )
        outcome.append(out)

    basis_list = [basis.name for basis in state.qkd_leader_basis_list]

    # FIXME: Send already binary basis instead of HV/AD.
    r = await http_client.post(f"http://{follower_node_address}/qkd/request_basis_list", json=basis_list)
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to request basis list from follower: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to request basis list from follower",
        )
    follower_basis_list = r.json()

    index_list = [i for i in range(len(follower_basis_list)) if follower_basis_list[i] == basis_list[i]]
    final_bits = [outcome[i] for i in index_list]

    logger.info("Final bits: %s", final_bits)

    return final_bits


@router.post("")
async def qkd(
    http_client: ClientDep,
    state: StateDep,
    timetagger_address: str | None = None,
) -> list[int]:
    """Perform a QKD protocol with the given follower node."""
    if not state.qkd_leader_basis_list:
        logger.error("QKD basis list is empty")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QKD basis list is empty",
        )

    return await _qkd(http_client, state, timetagger_address)


@router.post("/single_bit")
async def request_qkd_single_pass(state: StateDep) -> bool:
    client = Client(host=settings.router_address, port=settings.router_port, timeout=600_000)
    hwp = cast(
        "RotatorInstrument",
        client.get_device(settings.qkd_settings.request_hwp[0], settings.qkd_settings.request_hwp[1]),
    )

    if hwp is None:
        logger.error("Could not find half waveplate device")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find half waveplate device",
        )

    logger.debug("Halfwaveplate device found: %s", hwp)

    # Check if we have basis choices available
    if state.qkd_single_bit_current_index >= len(state.qkd_follower_basis_list):
        logger.error("No more basis choices available in follower basis list")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No more basis choices available in follower basis list",
        )

    # Get the basis choice from the follower basis list
    basis_choice = state.qkd_follower_basis_list[state.qkd_single_bit_current_index]
    state.qkd_single_bit_current_index += 1

    int_choice = secrets.randbits(1)  # FIXME: Make this real quantum random.

    state.qkd_request_basis_list.append(basis_choice)
    state.qkd_request_bit_list.append(int_choice)
    angle = basis_choice.angles[int_choice].value

    hwp.move_to(angle)

    return True


@router.post("/request_basis_list")
def request_qkd_basis_list(leader_basis_list: list[str], state: StateDep) -> list[str]:
    """Return the list of basis angles for QKD."""
    # Check that lengths match
    if len(leader_basis_list) != len(state.qkd_request_basis_list):
        logger.error("Length of leader basis list does not match length of request basis list")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Length of leader basis list does not match length of request basis list",
        )

    ret = [basis.name for basis in state.qkd_request_basis_list]
    index_list = [i for i in range(len(leader_basis_list)) if ret[i] == leader_basis_list[i]]
    final_bits = [state.qkd_request_bit_list[i] for i in index_list]
    logger.error("Final bits: %s", final_bits)

    state.qkd_request_basis_list.clear()
    state.qkd_request_bit_list.clear()

    return ret


@router.post("/set_emoji")
def set_emoji(emoji: str, state: StateDep) -> None:
    """Set the emoji pick for QKD."""
    state.qkd_emoji_pick = emoji


@router.get("/question_order")
async def request_question_order(
    state: StateDep,
    http_client: ClientDep,
) -> list[int]:
    """
    Return the question order for QKD.

    If this node is a leader, it generates a random question order and stores it in the state.
    If this node is a follower, it requests the question order from the leader node.
    Returns the question order as a list of integers.
    """
    if state.role not in (NodeRole.LEADER, NodeRole.FOLLOWER):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The node must be a leader or follower.",
        )

    # Return cached question order if already generated
    if len(state.qkd_question_order) > 0:
        return state.qkd_question_order

    # Leader node: generate question order
    if state.role == NodeRole.LEADER:
        if state.followers_address == "":
            logger.error("Leader node has no follower address set")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Leader node has no follower address set",
            )

        question_range = range(
            settings.qkd_settings.minimum_question_index, settings.qkd_settings.maximum_question_index + 1
        )
        question_order = random.sample(
            list(question_range), settings.qkd_settings.bitstring_length
        )  # just choosing question order, no need for secure secrets package.
        state.qkd_question_order = question_order
        return state.qkd_question_order

    # Node is a follower
    if state.leaders_address == "":
        logger.error("Follower node has no leader address set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Follower node has no leader address set",
        )
    r = await http_client.get(f"http://{state.leaders_address}/qkd/question_order")
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to get question order from leader: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get question order from leader",
        )
    state.qkd_question_order = r.json()
    return state.qkd_question_order


@router.get("/is_follower_ready")
async def is_follower_ready(state: StateDep) -> bool:
    """
    Check if the follower node is ready for QKD.

    Follower is ready when the state has the basis list with as many choices as the bitstring length.
    """
    if state.role != NodeRole.FOLLOWER:
        logger.error("Node is not a follower")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node is not a follower",
        )

    return len(state.qkd_follower_basis_list) == settings.qkd_settings.bitstring_length


@router.post("/submit_result")
async def submit_result(result: QKDResult, state: StateDep) -> None:
    """QKD leader calls this endpoint of the follower to submit the QKD result as well as the emoji chosen."""
    state.qkd_emoji_pick = result.emoji
    state.qkd_n_matching_bits = result.n_matching_bits
    qkd_result_received_event.set()  # Signal that the result has been received
    logger.info("Received QKD result from follower: %s", result)


async def _wait_for_follower_ready(state: NodeState, http_client: httpx.AsyncClient) -> None:
    """Poll the follower until it's ready, checking every 0.5 seconds."""
    ready = False
    first_503 = True
    while not ready:
        # Check if protocol was cancelled
        if protocol_cancelled_event.is_set():
            logger.warning("Protocol cancelled while waiting for follower ready")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Protocol cancelled by peer or user")

        r = await http_client.get(f"http://{state.followers_address}/qkd/is_follower_ready")

        # If the follower disconnects while the leader is waiting, the 503 error of `Node is not a follower` error might come before we can handle the cancellation event.
        if r.status_code == status.HTTP_503_SERVICE_UNAVAILABLE and first_503:
            logger.warning("Received QKD result from follower: %s", r)
            first_503 = False
            continue

        if r.status_code != status.HTTP_200_OK:
            logger.error("Failed to check if follower is ready: %s", r.text)
            raise HTTPException(
                status_code=r.status_code,
                detail=f"Failed to check if follower is ready, {r.text}",
            )

        ready = r.json()
        if not ready:
            logger.info("Follower is not ready yet, waiting.")
            await asyncio.sleep(0.4)  # Make sure this is smaller than the protocol_cancelled event clear timer.

    logger.info("Follower is ready")


async def _submit_result_to_follower(state: NodeState, http_client: httpx.AsyncClient, qkd_result: QKDResult) -> None:
    """Submit the QKD result to the follower node."""
    r = await http_client.post(f"http://{state.followers_address}/qkd/submit_result", json=qkd_result.model_dump())
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to submit QKD result to follower: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit QKD result to follower",
        )
    logger.info("Successfully submitted QKD result to follower")


async def _submit_basis_list_leader(
    state: NodeState, http_client: httpx.AsyncClient, basis_list: list[QKDEncodingBasis], timetagger_address: str
) -> QKDResult:
    state.qkd_leader_basis_list = basis_list
    await _wait_for_follower_ready(state, http_client)

    ret = await _qkd(http_client, state, timetagger_address, follower_node_address=state.followers_address)
    logger.info("Final QKD bits: %s", str(ret))

    # Assemble QKDResult object
    qkd_result = QKDResult(
        n_matching_bits=len(ret),
        n_total_bits=settings.qkd_settings.bitstring_length,
        emoji=state.qkd_emoji_pick,
        role="leader",
    )

    # Submit result to follower
    await _submit_result_to_follower(state, http_client, qkd_result)
    return qkd_result


async def _submit_basis_list_follower(state: NodeState, basis_list: list[QKDEncodingBasis]) -> QKDResult:
    state.qkd_follower_basis_list = basis_list

    # don't wait for the event if the result is already set. This avoids deadlocks in case the result was set before this function is called.
    if state.qkd_n_matching_bits == -1:
        # Wait for EITHER result OR cancellation
        _done, pending = await asyncio.wait(
            [
                asyncio.create_task(qkd_result_received_event.wait()),
                asyncio.create_task(protocol_cancelled_event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()

        # Check if protocol was cancelled
        if protocol_cancelled_event.is_set():
            logger.warning("Protocol cancelled while waiting for QKD result")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Protocol cancelled by peer or user")

    # Reassemble the QKDResult object from the state
    qkd_result = QKDResult(
        n_matching_bits=state.qkd_n_matching_bits,
        n_total_bits=settings.qkd_settings.bitstring_length,
        emoji=state.qkd_emoji_pick,
        role="follower",
    )

    # Clear the event for the next QKD run
    qkd_result_received_event.clear()

    logger.info("Follower received QKD result: %s", state.qkd_n_matching_bits)
    return qkd_result


@router.post("/submit_selection_and_start")
async def submit_qkd_selection_and_start_qkd(
    state: StateDep, http_client: ClientDep, basis_list: list[str], timetagger_address: str = ""
) -> QKDResult:
    """
    GUI calls this function to submit the QKD basis selection and start the QKD protocol.

    This call is called by both leader and follower, depending on the node role, different actions are taken.
    """
    if state.role == NodeRole.INDEPENDENT:
        logger.error("Node must be either leader or follower to start QKD")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Node must be either leader or follower to start QKD",
        )

    # Convert 'a' or 'b' strings to QKDEncodingBasis enum values
    qkd_basis_list = []
    for basis_str in basis_list:
        if basis_str.lower() == "a":
            qkd_basis_list.append(QKDEncodingBasis.HV)
        elif basis_str.lower() == "b":
            qkd_basis_list.append(QKDEncodingBasis.DA)
        else:
            logger.exception("Invalid basis string: %s. Expected 'a' or 'b'", basis_str)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid basis string: {basis_str}. Expected 'a' or 'b'",
            )

    if state.role == NodeRole.LEADER:
        if timetagger_address == "":
            logger.error("Leader must provide timetagger address to start QKD")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Leader must provide timetagger address to start QKD",
            )
        return await _submit_basis_list_leader(state, http_client, qkd_basis_list, timetagger_address)

    # If the node is not leading, it is assumed it is a follower due to previous check
    return await _submit_basis_list_follower(state, qkd_basis_list)
