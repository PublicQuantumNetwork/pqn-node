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

# Calibrated angles a little off today's raw ones, and fractional, so neither a raw move
# nor a rounded one can ever pass for a calibrated one.
CALIBRATED = {"h": 2.7, "v": 44.6, "d": 22.3, "a": -19.8}

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


def no_coincidences_rate(_leader_angle: float, _follower_angle: float) -> float:
    """Return the coincidences/s of a source that gives none: every count is zero."""
    return 0.0


class RateSequence:
    """A source that gives the coincidences/s the test chose, one per question, in order."""

    def __init__(self, rates: list[float]) -> None:
        self._rates = list(rates)

    def __call__(self, _leader_angle: float, _follower_angle: float) -> float:
        return self._rates.pop(0)


def _no_random_bit(_n_bits: int) -> int:
    """Stand-in for secrets.randbits: correlation mode has no random bit, so being asked is a failure."""
    message = "correlation mode has no random bit"
    raise AssertionError(message)


class FixedBits:
    """Stand-in for the module's secrets: hands out the bits the test chose, in order."""

    def __init__(self, bits: list[int]) -> None:
        self._bits = list(bits)

    def randbits(self, n_bits: int) -> int:
        message = "the random bit of a question is one bit"
        assert n_bits == 1, message
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
    """A leading Node with a fake waveplate, playing against the fake follower and time tagger.

    ``integration_times`` records what the time tagger was asked to integrate for, one entry
    per measurement, and ``plates`` holds waveplates the router serves by device name, for
    tests that tell two rotators apart.
    """

    def __init__(
        self,
        client: TestClient,
        plate: FakeWaveplate,
        follower: FakeFollower,
        integration_times: list[float],
        plates: dict[tuple[str, str], FakeWaveplate],
    ) -> None:
        self.client = client
        self.plate = plate
        self.follower = follower
        self.integration_times = integration_times
        self.plates = plates


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
        integration_times: list[float] = []
        # A waveplate per device name, for tests that tell two rotators apart; every device
        # the map does not name gets the one shared plate.
        plates: dict[tuple[str, str], FakeWaveplate] = {}

        class FakeRouterClient:
            def __init__(self, **_: object) -> None:
                pass

            def get_device(self, provider: str, name: str) -> FakeWaveplate:
                return plates.get((provider, name), plate)

        monkeypatch.setattr(hardware, "Client", FakeRouterClient)
        monkeypatch.setattr(settings, "follower_node_address", "follower.invalid:8000")

        def handler(request: httpx.Request) -> httpx.Response:
            follower.requests.append(request)
            if request.url.path == "/timetagger/measure_correlation":
                integration_time_s = float(request.url.params["integration_time_s"])
                integration_times.append(integration_time_s)
                # The leader's photons pass the waveplate its qkd_settings.hwp names: a test
                # that names the rotators apart registers that plate, others share one.
                leader_plate = plates.get(settings.qkd_settings.hwp, plate)
                counts = round(follower.source(leader_plate.moves[-1], follower.moves[-1]) * integration_time_s)
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
        return FakePair(TestClient(app), plate, follower, integration_times, plates)

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

    The follower answers left on every question, so a right answer lands on a medium VD
    level and shows up as a disagreement while the leader's moves stay exactly its own
    Answers', question after question through a six-question game in the last case.
    """

    answers: list[str]
    angles: list[str]
    agreement: list[int]


LEADER_MOVE_CASES = [
    LeaderMoveCase([LEFT, RIGHT], ["v", "d"], [1, 0]),
    LeaderMoveCase([RIGHT, LEFT], ["d", "v"], [0, 1]),
    LeaderMoveCase([LEFT, RIGHT, LEFT, RIGHT, LEFT, RIGHT], ["v", "d", "v", "d", "v", "d"], [1, 0, 1, 0, 1, 0]),
]


@pytest.mark.parametrize("case", LEADER_MOVE_CASES, ids=["left then right", "right then left", "six questions"])
def test_the_correlation_leader_moves_by_its_own_answers(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: LeaderMoveCase,
) -> None:
    """The leader's Answers alone move its own waveplate, left → V and right → D, whatever the follower answered.

    Its waveplate is the one named in qkd_settings.hwp, not the follower's request_hwp: with
    the two rotators named apart, only the hwp one moves. The follower answers left on every
    question, so a right answer lands on a medium VD level and shows up as a disagreement
    while the leader's moves stay exactly its own Answers', through a six-question game in
    the last case. The leader also starts with bits left over from an earlier entanglement
    game and no reset between games, which its correlation moves must ignore.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "hwp", ("provider", "leader"))
    monkeypatch.setattr(settings.qkd_settings, "request_hwp", ("provider", "request"))
    game_state.qkd_leader_basis_list = [ANSWER_TO_BASIS[answer] for answer in case.answers]
    # Bits an earlier entanglement game recorded, left in place by a missing reset.
    game_state.qkd_bit_list = [0, 1]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[LEFT] * len(case.answers)))
    leader_plate = FakeWaveplate()
    request_plate = FakeWaveplate()
    game.plates[("provider", "leader")] = leader_plate
    game.plates[("provider", "request")] = request_plate

    response = game.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == case.agreement
    assert leader_plate.moves == [CALIBRATED[name] for name in case.angles]
    assert request_plate.moves == []
    # A correlation game neither consumes nor records random bits.
    assert game_state.qkd_bit_list == [0, 1]
    assert_answers_never_fetched(game.follower)


def test_the_correlation_follower_moves_by_its_own_answers(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's Answers alone move its own waveplate, left → V and right → D, one move per handshake.

    Its waveplate is the one named in request_hwp, not the leader's hwp: with the two
    rotators named apart, only the request one moves, on every question of a six-question
    game. Decision 3: the leader never fetches the follower's Answers in correlation mode,
    so the follower records none of them for it.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "hwp", ("provider", "leader"))
    monkeypatch.setattr(settings.qkd_settings, "request_hwp", ("provider", "request"))
    game_state.qkd_follower_basis_list = [
        QKDEncodingBasis.DA,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.HV,
    ]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[]))
    leader_plate = FakeWaveplate()
    request_plate = FakeWaveplate()
    game.plates[("provider", "leader")] = leader_plate
    game.plates[("provider", "request")] = request_plate

    responses = [game.client.post("/qkd/single_bit") for _ in game_state.qkd_follower_basis_list]

    assert all(response.status_code == HTTPStatus.OK for response in responses)
    assert [response.json() for response in responses] == [True] * len(responses)
    assert request_plate.moves == [CALIBRATED[name] for name in ["d", "v", "d", "v", "d", "v"]]
    assert leader_plate.moves == []
    assert game_state.qkd_request_basis_list == []
    assert game_state.qkd_request_bit_list == []


def test_a_one_question_follower_correlation_game(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single Answer is a whole follower game in correlation mode: one move by that Answer, nothing recorded.

    A one-question game must not fall back to the entanglement path, whose random bit and
    recorded Answers belong to today's game alone (decisions 1, 3 and 4).
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    game_state.qkd_follower_basis_list = [QKDEncodingBasis.DA]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[]))

    response = game.client.post("/qkd/single_bit")

    assert response.status_code == HTTPStatus.OK
    assert response.json() is True
    assert game.plate.moves == [CALIBRATED["d"]]
    assert game_state.qkd_request_basis_list == []
    assert game_state.qkd_request_bit_list == []


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
    (1300.0, CoincidenceLevel.MEDIUM),
    (1200.0, CoincidenceLevel.MEDIUM),
    (1000.0, CoincidenceLevel.LOW),
    (500.0, CoincidenceLevel.LOW),
    # Beyond the outer Reference rates.
    (6000.0, CoincidenceLevel.HIGH),
    (300.0, CoincidenceLevel.LOW),
    (0.0, CoincidenceLevel.LOW),
    # Just either side of both midpoints (2250 and 1125): the exact value decides.
    (2250.5, CoincidenceLevel.HIGH),
    (2249.5, CoincidenceLevel.MEDIUM),
    (1125.5, CoincidenceLevel.MEDIUM),
    (1124.5, CoincidenceLevel.LOW),
    # Exactly midway, where the nearest is tied: the higher of the two tied references wins.
    (2250.0, CoincidenceLevel.HIGH),
    (1125.0, CoincidenceLevel.MEDIUM),
]


@pytest.mark.parametrize(("rate", "expected"), CLASSIFICATIONS)
def test_a_rate_is_classified_by_its_nearest_reference_rate(rate: float, expected: CoincidenceLevel) -> None:
    """A rate lands on its nearest of the three Reference rates: at, between and beyond them."""
    assert _coincidence_level(rate) is expected


def test_classification_follows_the_configured_reference_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 7: the nearest of this Node's own Reference rates, whatever they are set to.

    VV is its own configured value here, not twice VD (2800 where twice 1500 is 3000), and
    2200 cps is nearer VV than VD only with the real 2800.
    """
    rates = settings.correlation_settings.reference_rates
    monkeypatch.setattr(rates, "vv", 2800.0)
    monkeypatch.setattr(rates, "vd", 1500.0)
    monkeypatch.setattr(rates, "dd", 750.0)

    assert _coincidence_level(2800.0) is CoincidenceLevel.HIGH
    assert _coincidence_level(2200.0) is CoincidenceLevel.HIGH
    assert _coincidence_level(2000.0) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(1200.0) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(1000.0) is CoincidenceLevel.LOW
    assert _coincidence_level(750.0) is CoincidenceLevel.LOW
    assert _coincidence_level(100.0) is CoincidenceLevel.LOW
    assert _coincidence_level(5000.0) is CoincidenceLevel.HIGH


def test_classification_keeps_the_reference_rates_full_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classify by fractional Reference rates exactly as they are set, not truncated or rounded.

    Any float is a valid Reference rate (decision 16, agent decision A5), and the midpoints
    move with the fractions: with VV 3000.6, VD 1503.4 and DD 750.2 the VV/VD midpoint is
    2252.0 and the VD/DD one 1126.8, so a rate just below either lands on the lower side.
    """
    rates = settings.correlation_settings.reference_rates
    monkeypatch.setattr(rates, "vv", 3000.6)
    monkeypatch.setattr(rates, "vd", 1503.4)
    monkeypatch.setattr(rates, "dd", 750.2)

    assert _coincidence_level(3000.6) is CoincidenceLevel.HIGH
    assert _coincidence_level(1503.4) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(750.2) is CoincidenceLevel.LOW
    assert _coincidence_level(2251.85) is CoincidenceLevel.MEDIUM
    assert _coincidence_level(1125.95) is CoincidenceLevel.LOW


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
    # A non-default integration time, so a rate divided by any other time misclassifies, and a
    # different one in the QKD settings, so measuring with the wrong config shows.
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    monkeypatch.setattr(settings.qkd_settings, "measurement_config", MeasurementConfig(integration_time_s=3))
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
    # Every question was measured for the CHSH config's 2 s, not the QKD settings' 3 s or any
    # other time, and each count became a rate with that same time.
    assert game.integration_times == [2.0, 2.0, 2.0, 2.0]
    # A correlation game records no random bits of its own, and its Answers stay put.
    assert game_state.qkd_bit_list == []
    assert game_state.qkd_leader_basis_list == [ANSWER_TO_BASIS[answer] for answer in [LEFT, LEFT, RIGHT, RIGHT]]
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
    monkeypatch.setattr(settings.qkd_settings, "hwp", ("provider", "leader"))
    monkeypatch.setattr(settings.qkd_settings, "request_hwp", ("provider", "request"))
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT, RIGHT, RIGHT, LEFT],
        bits=[1, 0, 1, 0],
        source=entangled_rate,
    )
    game = make_game(follower)
    leader_plate = FakeWaveplate()
    request_plate = FakeWaveplate()
    game.plates[("provider", "leader")] = leader_plate
    game.plates[("provider", "request")] = request_plate
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT, RIGHT, LEFT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 2, "n_total_bits": 4, "emoji": "🙈", "role": "leader"}
    # The random bit still picks the state within each Answer's basis, on both sides, and
    # the leader moves only its own hwp rotator.
    assert leader_plate.moves == [CALIBRATED[name] for name in ["h", "a", "h", "a"]]
    assert request_plate.moves == []
    assert follower.moves == [CALIBRATED[name] for name in ["v", "d", "a", "h"]]
    # The leader recorded exactly the random bits it moved by, one per question, and its
    # Answers stayed put, as they do today.
    assert game_state.qkd_bit_list == [0, 1, 0, 1]
    assert game_state.qkd_leader_basis_list == [ANSWER_TO_BASIS[answer] for answer in [LEFT, RIGHT, LEFT, RIGHT]]
    assert follower.results == [response.json()]
    # Today's game still fetches the follower's Answers.
    assert any(request.url.path == "/qkd/request_basis_list" for request in follower.requests)


def test_an_entanglement_game_where_no_answer_matches(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entanglement game where no Answer matches reports zero matching bits (decision 4).

    Every question picks a different basis on the two sides, so the leader's fetch of the
    follower's Answers finds nothing to keep and today's count is an honest 0.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0, 1]))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 2)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(mode=OperationMode.ENTANGLEMENT, answers=[RIGHT, RIGHT], bits=[0, 1], source=entangled_rate)
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT, LEFT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 0, "n_total_bits": 2, "emoji": "🙈", "role": "leader"}
    assert follower.results == [response.json()]
    assert any(request.url.path == "/qkd/request_basis_list" for request in follower.requests)


def test_a_one_question_entanglement_game(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-question entanglement game stays today's game: the key bits, not the agreement (decision 4).

    The two Answers match, so today's count is 1 whatever the photons did; the scripted
    count lands on a medium Coincidence level (1500 cps), which a correlation-style
    agreement would count as 0. Today's game still fetches the follower's Answers.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([1]))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 1)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT],
        bits=[1],
        source=RateSequence([1500.0]),
    )
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 1, "n_total_bits": 1, "emoji": "🙈", "role": "leader"}
    assert follower.results == [response.json()]
    assert any(request.url.path == "/qkd/request_basis_list" for request in follower.requests)


def test_an_entanglement_game_with_more_than_eight_matching_answers(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entanglement game with nine matching Answers reports all nine (decision 4).

    Every Answer matches, so today's count is the question count, however long the game.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0] * 9))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 9)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT] * 9,
        bits=[1] * 9,
        source=entangled_rate,
    )
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT] * 9)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 9, "n_total_bits": 9, "emoji": "🙈", "role": "leader"}
    assert follower.results == [response.json()]
    assert any(request.url.path == "/qkd/request_basis_list" for request in follower.requests)


def test_a_one_question_correlation_game_that_disagrees(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single question is a whole game: its VD level disagrees, n_matching_bits is 0, and nothing is fetched.

    One question on either side of the matching count keeps the game from silently falling
    back to counting questions instead of agreements.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 1)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(mode=OperationMode.CORRELATION, answers=[RIGHT])
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 0, "n_total_bits": 1, "emoji": "🙈", "role": "leader"}
    assert game.plate.moves == [CALIBRATED["v"]]
    assert follower.moves == [CALIBRATED["d"]]
    assert_answers_never_fetched(follower)


def test_a_correlation_game_where_nothing_coincides(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that gives no coincidences still plays: every zero count is a low level, so every question agrees.

    The two sides' Answers disagree on every question, so with a bright source this game
    would disagree everywhere; the zero counts alone make it agree, and the game still
    never fetches the follower's Answers.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", 3)
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(
        mode=OperationMode.CORRELATION,
        answers=[RIGHT, LEFT, RIGHT],
        source=no_coincidences_rate,
    )
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, [LEFT, RIGHT, LEFT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"n_matching_bits": 3, "n_total_bits": 3, "emoji": "🙈", "role": "leader"}
    assert_answers_never_fetched(follower)


class AgreementCase(NamedTuple):
    """One whole-game agreement case: both sides' Answers and the matching count they give."""

    leader_answers: list[str]
    follower_answers: list[str]
    n_matching_bits: int


AGREEMENT_CASES = [
    # Every question VD or DV: a medium level, so every question disagrees.
    AgreementCase([LEFT, LEFT, RIGHT, RIGHT], [RIGHT, RIGHT, LEFT, LEFT], 0),
    # VV, VV, DD, DD: high, high, low, low, so every question agrees.
    AgreementCase([LEFT, LEFT, RIGHT, RIGHT], [LEFT, LEFT, RIGHT, RIGHT], 4),
]


@pytest.mark.parametrize("case", AGREEMENT_CASES, ids=["every question disagrees", "every question agrees"])
def test_a_correlation_game_whose_questions_all_agree_or_all_disagree(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: AgreementCase,
) -> None:
    """A game where every Coincidence level says the same thing reports 0 or every question, not a stand-in.

    Zero matches and all-questions-matching are the two counts a length or modulo could
    silently stand in for.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", len(case.leader_answers))
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    follower = FakeFollower(mode=OperationMode.CORRELATION, answers=case.follower_answers)
    game = make_game(follower)
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    response = start_ssm_game(game, case.leader_answers)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "n_matching_bits": case.n_matching_bits,
        "n_total_bits": len(case.leader_answers),
        "emoji": "🙈",
        "role": "leader",
    }
    assert_answers_never_fetched(follower)


def test_the_rate_keeps_its_full_precision_through_the_game(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate within 0.1 cps of a Reference midpoint classifies on its exact value, not a rounded one.

    With the DD Reference at 749.6 cps the VD/DD midpoint sits at 1124.8 cps. Two questions
    land at 1124.75 cps (just below: low, agreed) and 1124.875 cps (just above: medium,
    disagreed), so flooring the rate, rounding it, being off by one count, or flooring the
    distances all flip one of them.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.correlation_settings.reference_rates, "dd", 749.6)
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=8))
    game_state.qkd_leader_basis_list = [QKDEncodingBasis.HV, QKDEncodingBasis.DA]
    # Source rates of 1124.75 and 1124.875 cps: counts 8998 and 8999 at the 8 s integration.
    game = make_game(
        FakeFollower(mode=OperationMode.CORRELATION, answers=[LEFT, RIGHT], source=RateSequence([1124.75, 1124.875]))
    )

    response = game.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})

    assert response.status_code == HTTPStatus.OK
    assert game.integration_times == [8.0, 8.0]
    assert response.json() == [1, 0]


def test_a_fractional_integration_time_divides_the_count_exactly(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fractional integration time is asked for and divided out exactly as it is configured.

    The configured 0.25 s is below 1 s and not a multiple of 0.1 s, so a time rounded to
    tenths (0.2 s) or floored at a second (1 s) shows in what the time tagger was asked
    for. A source giving 2000 cps over the 0.25 s counts 500, whose rate is 2000 cps
    (medium → disagreed); classifying the raw count instead would give 500 cps, the low
    level (agreed).
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=0.25))
    game_state.qkd_leader_basis_list = [QKDEncodingBasis.HV]
    game = make_game(FakeFollower(mode=OperationMode.CORRELATION, answers=[LEFT], source=RateSequence([2000.0])))

    response = game.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})

    assert response.status_code == HTTPStatus.OK
    assert game.integration_times == [0.25]
    assert response.json() == [0]


class CorrelationGameCase(NamedTuple):
    """One table-driven correlation game: its Reference rates, integration time and counts.

    The counts are the time tagger's, one per question, scripted through the source; most
    cases have equal first and last counts with a different level between them, and the
    last two run a default-length and a nine-question game.
    """

    name: str
    references: dict[str, float]
    integration_time_s: float
    counts: list[int]


CORRELATION_GAME_CASES = [
    CorrelationGameCase(
        "default references, sub-second integration", {"vv": 3000.0, "vd": 1500.0, "dd": 750.0}, 0.25, [500, 750, 500]
    ),
    CorrelationGameCase(
        "default references, a rate above VD nearest VD",
        {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
        2.5,
        [4500, 7500, 4500],
    ),
    CorrelationGameCase("VV not twice VD", {"vv": 2800.0, "vd": 1500.0, "dd": 750.0}, 1.0, [2200, 1200, 2200]),
    CorrelationGameCase("DD above half of VD", {"vv": 3000.0, "vd": 1500.0, "dd": 900.0}, 2.5, [2875, 3750, 2875]),
    CorrelationGameCase("DD at zero", {"vv": 3000.0, "vd": 1500.0, "dd": 0.0}, 1.0, [0, 1500, 0]),
    CorrelationGameCase(
        "fractional references, minute-long integration",
        {"vv": 3000.6, "vd": 1503.4, "dd": 750.2},
        60.0,
        [135111, 90204, 67557],
    ),
    CorrelationGameCase(
        "VD not floored at twice DD", {"vv": 3000.0, "vd": 1500.0, "dd": 900.0}, 1.0, [2300, 1500, 2300]
    ),
    CorrelationGameCase(
        "VV not capped at 2.5 times VD", {"vv": 4500.0, "vd": 1500.0, "dd": 750.0}, 1.0, [2800, 4500, 2800]
    ),
    CorrelationGameCase(
        "rates beyond ten thousand", {"vv": 40000.0, "vd": 20000.0, "dd": 10000.0}, 1.0, [40000, 20000, 40000]
    ),
    CorrelationGameCase(
        "the high/medium boundary within a tenth of a cps",
        {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
        10.0,
        [22499, 22500, 22499],
    ),
    CorrelationGameCase(
        "six questions, the default game length",
        {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
        1.0,
        [3000, 1500, 3000, 1500, 3000, 1500],
    ),
    CorrelationGameCase(
        "nine questions, an agreement after the eighth",
        {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
        1.0,
        [3000, 1500, 3000, 1500, 3000, 1500, 3000, 1500, 3000],
    ),
]


def expected_agreement(case: CorrelationGameCase) -> list[int]:
    """Compute a case's expected agreement independently of the Node's own classification.

    Each count becomes a rate with the case's integration time, the nearest of the three
    Reference rates wins, and medium is the only disagreement.
    """
    agreement = []
    for count in case.counts:
        rate = count / case.integration_time_s
        nearest = min(
            (
                (case.references["vv"], "high"),
                (case.references["vd"], "medium"),
                (case.references["dd"], "low"),
            ),
            key=lambda reference: abs(reference[0] - rate),
        )
        agreement.append(int(nearest[1] != "medium"))
    return agreement


@pytest.mark.parametrize("case", CORRELATION_GAME_CASES, ids=[case.name for case in CORRELATION_GAME_CASES])
def test_a_correlation_game_classifies_each_count_by_its_nearest_reference(
    make_game: Callable[[FakeFollower], FakePair],
    game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: CorrelationGameCase,
) -> None:
    """A whole-game table: Reference-rate sets, integration times and per-question counts.

    Each case runs the raw protocol for its per-question agreement and then the whole game
    for the n_matching_bits that agreement sums to, with the time tagger scripting the
    case's counts at the case's integration time.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))
    for name, rate in case.references.items():
        monkeypatch.setattr(settings.correlation_settings.reference_rates, name, rate)
    monkeypatch.setattr(
        settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=case.integration_time_s)
    )
    monkeypatch.setattr(settings.qkd_settings, "bitstring_length", len(case.counts))
    game_state.role = NodeRole.LEADER
    game_state.followers_address = "follower.invalid:8000"
    # A rate per count, so the time tagger returns exactly the case's counts; enough for
    # both the raw protocol and the whole game.
    follower = FakeFollower(
        mode=OperationMode.CORRELATION,
        answers=[LEFT] * (2 * len(case.counts)),
        source=RateSequence([count / case.integration_time_s for count in case.counts] * 2),
    )
    game = make_game(follower)
    game_state.qkd_leader_basis_list = [QKDEncodingBasis.HV] * len(case.counts)
    protocol = game.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})
    assert protocol.status_code == HTTPStatus.OK
    assert game.client.post("/qkd/set_emoji", params={"emoji": "🙈"}).status_code == HTTPStatus.OK

    result = start_ssm_game(game, [LEFT] * len(case.counts))

    assert result.status_code == HTTPStatus.OK
    assert protocol.json() == expected_agreement(case)
    assert result.json() == {
        "n_matching_bits": sum(expected_agreement(case)),
        "n_total_bits": len(case.counts),
        "emoji": "🙈",
        "role": "leader",
    }
    # Every question of both runs was measured for the case's integration time.
    assert game.integration_times == [case.integration_time_s] * (2 * len(case.counts))
    assert_answers_never_fetched(follower)
