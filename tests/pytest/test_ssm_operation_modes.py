"""Tests for the Share a Secret Message game in both Operation modes.

Per the plan's testing rule, kind 2: the leader is the real Node with its waveplate faked
through ``hardware.Client``, while the follower and the time tagger answer on an
``httpx.MockTransport``. The time tagger is a source model that counts coincidences from
the two waveplate angles: a product-VV source in correlation mode, a |Φ⁺⟩ entangled state
in entanglement mode.

What the photons say in correlation mode (decision 1): with each side at V or D by its own
Answer, a product-VV source gives VV high, VD/DV medium and DD low coincidences, and high
or low means the two Answers agreed while medium means they did not.
"""

import json
import math
from collections.abc import AsyncGenerator
from collections.abc import Callable
from collections.abc import Iterator
from http import HTTPStatus
from types import SimpleNamespace
from typing import NamedTuple

import httpx
import pytest
from fastapi.testclient import TestClient
from pqn_hardware.measurement import MeasurementConfig

from pqn_node.api import hardware
from pqn_node.api.deps import get_http_client
from pqn_node.api.routes import qkd
from pqn_node.api.routes.qkd import _coincidence_level
from pqn_node.constants import CoincidenceLevel
from pqn_node.constants import OperationMode
from pqn_node.constants import QKDEncodingBasis
from pqn_node.core.config import NodeRole
from pqn_node.core.config import NodeState
from pqn_node.core.config import PolarizationAngles
from pqn_node.core.config import get_state
from pqn_node.core.config import settings
from pqn_node.main import app

# Calibrated angles a little off today's raw ones, so a raw move can never pass for a
# calibrated one; the same values as the Phase 1 move tests.
CALIBRATED = {"h": 3.0, "v": 44.0, "d": 20.0, "a": -20.0}

# An Answer as the GUI sends it: 'a' is left, 'b' is right; left → V (HV), right → D (DA).
LEFT = "a"
RIGHT = "b"
ANSWER_TO_BASIS = {LEFT: QKDEncodingBasis.HV, RIGHT: QKDEncodingBasis.DA}

# A product-VV source at ~3000 coincidences/s, the default high Reference rate.
VV_RATE = 3000.0


def _polarization(waveplate_angle: float) -> float:
    """Return the polarization a half waveplate angle sets: twice the angle, on the 0-360° axis."""
    return (2.0 * waveplate_angle) % 360.0


def _v_passes(waveplate_angle: float) -> float:
    """Return how often a V photon passes the analyzer this waveplate angle sets: sin² of its polarization."""
    return math.sin(math.radians(_polarization(waveplate_angle))) ** 2


def product_vv_rate(leader_angle: float, follower_angle: float) -> float:
    """Return the coincidences/s of a product-VV source: each photon is V and passes on its own."""
    return VV_RATE * _v_passes(leader_angle) * _v_passes(follower_angle)


def entangled_rate(leader_angle: float, follower_angle: float) -> float:
    """Return the coincidences/s of a |Φ⁺⟩ = (|HH⟩ + |VV⟩)/√2 source at one pair of PBS outputs."""
    difference = math.radians(_polarization(leader_angle) - _polarization(follower_angle))
    return VV_RATE * math.cos(difference) ** 2


def _no_random_bit(_n_bits: int) -> int:
    """Stand-in for secrets.randbits: correlation mode has no random bit, so being asked is a failure."""
    message = "correlation mode has no random bit"
    raise AssertionError(message)


class FixedBits:
    """Stand-in for the module's secrets: hands out the bits the test chose, in order."""

    def __init__(self, bits: list[int]) -> None:
        self._bits = list(bits)

    def randbits(self, _n_bits: int) -> int:
        return self._bits.pop(0)


class FakeWaveplate:
    def __init__(self) -> None:
        self.moves: list[float] = []

    def move_to(self, angle: float) -> None:
        self.moves.append(angle)


class FakeFollower:
    """The other Node: answers the follower endpoints, moving its own waveplate by its own Answers."""

    def __init__(
        self,
        mode: OperationMode,
        answers: list[str],
        bits: list[int] | None = None,
        source: Callable[[float, float], float] = product_vv_rate,
    ) -> None:
        self.mode = mode
        self.answers = answers
        self.bits = [] if bits is None else bits
        self.source = source
        # Its own Calibrated angles: a different Node, so its own settings values.
        self.angles = PolarizationAngles(**CALIBRATED)
        self.moves: list[float] = []
        self.requests: list[httpx.Request] = []
        self.results: list[dict] = []

    def answer_single_bit(self) -> None:
        """Move this Node's own waveplate for the next question, as its own /qkd/single_bit does."""
        answer = self.answers[len(self.moves)]
        if self.mode is OperationMode.CORRELATION:
            # Decision 1: the Answer alone picks V or D, with no random bit.
            self.moves.append(self.angles.v if answer == LEFT else self.angles.d)
            return
        bit = self.bits[len(self.moves)]
        self.moves.append(self.angles.waveplate_angle(ANSWER_TO_BASIS[answer].angles[bit]))

    def basis_list_response(self) -> httpx.Response:
        """Answer the leader's fetch of this Node's Answers, which only entanglement mode makes."""
        if self.mode is OperationMode.CORRELATION:
            # Decision 3: the leader must never fetch the follower's Answers in correlation
            # mode, so the fetch is answered with a failure rather than happily.
            detail = "the follower's Answers are never fetched in correlation mode"
            return httpx.Response(500, json=detail)
        return httpx.Response(200, json=[ANSWER_TO_BASIS[answer].name for answer in self.answers])

    def respond(self, request: httpx.Request) -> httpx.Response | None:
        """Answer one call to this Node's own endpoints, or None when the call is not to one of them."""
        if request.url.path == "/qkd/is_follower_ready":
            return httpx.Response(200, json=True)
        if request.url.path == "/qkd/single_bit":
            self.answer_single_bit()
            return httpx.Response(200, json=True)
        if request.url.path == "/qkd/request_basis_list":
            return self.basis_list_response()
        if request.url.path == "/qkd/submit_result":
            self.results.append(json.loads(request.read()))
            return httpx.Response(200)
        return None


class FakePair:
    """A leading Node with a fake waveplate, playing against the fake follower and time tagger."""

    def __init__(self, client: TestClient, plate: FakeWaveplate, follower: FakeFollower) -> None:
        self.client = client
        self.plate = plate
        self.follower = follower


@pytest.fixture(autouse=True)
def calibrated(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Set the Calibrated angles on the live settings, restored afterwards."""
    for name, angle in CALIBRATED.items():
        monkeypatch.setattr(settings.polarization_angles, name, angle)
    return CALIBRATED


@pytest.fixture
def game_state() -> Iterator[NodeState]:
    """Borrow the module-global game state, cleared, and put the originals back."""
    state = get_state()
    saved = (
        state.role,
        state.followers_address,
        state.qkd_emoji_pick,
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        state.qkd_follower_basis_list,
        state.qkd_request_basis_list,
        state.qkd_request_bit_list,
        state.qkd_single_bit_current_index,
        state.qkd_n_matching_bits,
    )
    state.role = NodeRole.INDEPENDENT
    state.followers_address = ""
    state.qkd_emoji_pick = ""
    state.qkd_leader_basis_list = []
    state.qkd_bit_list = []
    state.qkd_follower_basis_list = []
    state.qkd_request_basis_list = []
    state.qkd_request_bit_list = []
    state.qkd_single_bit_current_index = 0
    state.qkd_n_matching_bits = -1
    yield state
    (
        state.role,
        state.followers_address,
        state.qkd_emoji_pick,
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        state.qkd_follower_basis_list,
        state.qkd_request_basis_list,
        state.qkd_request_bit_list,
        state.qkd_single_bit_current_index,
        state.qkd_n_matching_bits,
    ) = saved


@pytest.fixture
def make_game(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[FakeFollower], FakePair]]:
    """Build the leading Node for a game: a fake waveplate, and a follower and time tagger on a mock transport."""

    def make(follower: FakeFollower) -> FakePair:
        plate = FakeWaveplate()

        class FakeRouterClient:
            def __init__(self, **_: object) -> None:
                pass

            def get_device(self, *_: object) -> FakeWaveplate:
                return plate

        monkeypatch.setattr(hardware, "Client", FakeRouterClient)
        monkeypatch.setattr(settings, "follower_node_address", "follower.invalid:8000")

        def handler(request: httpx.Request) -> httpx.Response:
            follower.requests.append(request)
            if request.url.path == "/timetagger/measure_correlation":
                integration_time_s = float(request.url.params["integration_time_s"])
                counts = round(follower.source(plate.moves[-1], follower.moves[-1]) * integration_time_s)
                return httpx.Response(200, json=counts)
            response = follower.respond(request)
            if response is None:
                return httpx.Response(404, json=f"unexpected call: {request.url.path}")
            return response

        async def fake_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                yield http_client

        app.dependency_overrides[get_http_client] = fake_http_client
        # No `with` block on the TestClient: the lifespan startup health check probes real hardware.
        return FakePair(TestClient(app), plate, follower)

    yield make
    app.dependency_overrides.pop(get_http_client, None)


def start_ssm_game(game: FakePair, answers: list[str]) -> httpx.Response:
    """Submit the leader's Answers and start the game, as the GUI does."""
    return game.client.post(
        "/qkd/submit_selection_and_start",
        params={"timetagger_address": "tt.invalid:9000"},
        json=answers,
    )


def assert_answers_never_fetched(follower: FakeFollower) -> None:
    """Decision 3: a correlation-mode leader never fetches the follower's Answers."""
    assert all(request.url.path != "/qkd/request_basis_list" for request in follower.requests)


# --------------------------------------------------------------------------------------
# The moves: each Answer moves only that player's own waveplate, with no random bit.
# --------------------------------------------------------------------------------------


class LeaderMoveCase(NamedTuple):
    """One leader move case: its Answers, the Calibrated angles they move to, and the agreement bits.

    The follower answers left twice, so a right answer lands on a medium VD level and shows
    up as a disagreement while the leader's moves stay exactly its own Answers'.
    """

    answers: list[str]
    angles: list[str]
    agreement: list[int]


LEADER_MOVE_CASES = [
    LeaderMoveCase([LEFT, RIGHT], ["v", "d"], [1, 0]),
    LeaderMoveCase([RIGHT, LEFT], ["d", "v"], [0, 1]),
]


@pytest.mark.parametrize("case", LEADER_MOVE_CASES, ids=["left then right", "right then left"])
def test_the_correlation_leader_moves_by_its_own_answers(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: LeaderMoveCase,
) -> None:
    """The leader's Answers alone move its own waveplate, left → V and right → D, whatever the follower answered."""
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    game_state.qkd_leader_basis_list = [ANSWER_TO_BASIS[answer] for answer in case.answers]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[LEFT, LEFT]))

    response = game.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == case.agreement
    assert game.plate.moves == [CALIBRATED[name] for name in case.angles]
    assert_answers_never_fetched(game.follower)


def test_the_correlation_follower_moves_by_its_own_answers(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's Answers alone move its own waveplate, left → V and right → D, one move per handshake."""
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    game_state.qkd_follower_basis_list = [QKDEncodingBasis.DA, QKDEncodingBasis.HV]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[]))

    responses = [game.client.post("/qkd/single_bit") for _ in game_state.qkd_follower_basis_list]

    assert [response.status_code for response in responses] == [HTTPStatus.OK, HTTPStatus.OK]
    assert [response.json() for response in responses] == [True, True]
    assert game.plate.moves == [CALIBRATED["d"], CALIBRATED["v"]]


# --------------------------------------------------------------------------------------
# The classification: a rate is classified by the nearest Reference rate (decision 7).
# --------------------------------------------------------------------------------------

CLASSIFICATIONS = [
    # At each Reference rate.
    (3000.0, CoincidenceLevel.HIGH),
    (1500.0, CoincidenceLevel.MEDIUM),
    (750.0, CoincidenceLevel.LOW),
    # Between two Reference rates: whichever is nearer.
    (2500.0, CoincidenceLevel.HIGH),
    (2000.0, CoincidenceLevel.MEDIUM),
    (1200.0, CoincidenceLevel.MEDIUM),
    (500.0, CoincidenceLevel.LOW),
    # Beyond the outer Reference rates.
    (6000.0, CoincidenceLevel.HIGH),
    (300.0, CoincidenceLevel.LOW),
    (0.0, CoincidenceLevel.LOW),
    # Exactly midway, where the nearest is tied: the higher of the two tied references wins.
    (2250.0, CoincidenceLevel.HIGH),
    (1125.0, CoincidenceLevel.MEDIUM),
]


@pytest.mark.parametrize(("rate", "expected"), CLASSIFICATIONS)
def test_a_rate_is_classified_by_its_nearest_reference_rate(rate: float, expected: CoincidenceLevel) -> None:
    """A rate lands on its nearest of the three Reference rates: at, between and beyond them."""
    assert _coincidence_level(rate) is expected


def test_classification_follows_the_configured_reference_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 7: the nearest of this Node's own Reference rates, whatever they are set to."""
    rates = settings.correlation_settings.reference_rates
    monkeypatch.setattr(rates, "vv", 200.0)
    monkeypatch.setattr(rates, "vd", 80.0)
    monkeypatch.setattr(rates, "dd", 20.0)

    assert _coincidence_level(200.0) is CoincidenceLevel.HIGH
    assert _coincidence_level(150.0) is CoincidenceLevel.HIGH
    assert _coincidence_level(80.0) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(100.0) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(60.0) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(20.0) is CoincidenceLevel.LOW
    assert _coincidence_level(10.0) is CoincidenceLevel.LOW
    assert _coincidence_level(5000.0) is CoincidenceLevel.HIGH


# --------------------------------------------------------------------------------------
# A game end to end, in both Operation modes.
# --------------------------------------------------------------------------------------


def test_a_correlation_game_end_to_end(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One game where the photons do all the telling: VV and DD agree, VD and DV do not.

    Decisions 1, 3, 7 and 17: both waveplates move by the Answers alone, each count becomes
    a rate with the integration time it was measured with, and the number of questions
    whose Coincidence level says "agreed" is the n_matching_bits of an unchanged QKDResult.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 4)
    # A non-default integration time, so a rate divided by any other time misclassifies.
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(mode=OperationMode.CORRELATION, answers=[LEFT, RIGHT, LEFT, RIGHT])
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT, LEFT, RIGHT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 2, "n_total_bits": 4, "emoji": "🙈", "role": "leader"}
    # Each side's own Answers moved only its own waveplate: left → V, right → D.
    assert game.plate.moves == [CALIBRATED[name] for name in ["v", "v", "d", "d"]]
    assert follower.moves == [CALIBRATED[name] for name in ["v", "d", "v", "d"]]
    # The follower got the same result, one handshake per question, and its Answers were never fetched.
    assert follower.results == [response.json()]
    assert_answers_never_fetched(follower)
    paths = [request.url.path for request in follower.requests]
    assert paths.count("/qkd/single_bit") == len(follower.answers)
    assert "/qkd/is_follower_ready" in paths


def test_an_entanglement_game_end_to_end(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entanglement mode keeps today's game (decision 4).

    A random bit still picks the state within each Answer's basis on both sides, the leader
    still fetches the follower's Answers, and n_matching_bits still counts the questions
    where the two Answers picked the same basis.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0, 1, 0, 1]))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 4)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT, RIGHT, RIGHT, LEFT],
        bits=[1, 0, 1, 0],
        source=entangled_rate,
    )
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT, RIGHT, LEFT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 2, "n_total_bits": 4, "emoji": "🙈", "role": "leader"}
    # The random bit still picks the state within each Answer's basis, on both sides.
    assert game.plate.moves == [CALIBRATED[name] for name in ["h", "a", "h", "a"]]
    assert follower.moves == [CALIBRATED[name] for name in ["v", "d", "a", "h"]]
    assert follower.results == [response.json()]
    # Today's game still fetches the follower's Answers.
    assert any(request.url.path == "/qkd/request_basis_list" for request in follower.requests)
