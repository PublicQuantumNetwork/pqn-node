"""The ``Whobot`` base class: its Actions, and the flow that runs them.

Whobot is the operations bot for a PQN Network. One instance serves many Nodes, letting an
operator probe and control any of them from a Chat Platform. This module is the
platform-independent half: what Whobot can do, and the flow that decides what to ask for
next. Rendering lives in a subclass such as ``whobot_slack.py``; the machinery an Action is
declared with, and the result vocabulary it returns, live in ``actions.py``.

Actions
-------

An Action is one thing Whobot can do, and it is the unit of extension: adding one means
adding a single ``@action``-decorated method to this class. No menu code is edited and no
Chat Platform code is touched, because the menu entry, the parameter form and the help text
are all derived from the declaration and the signature::

    @action(label="Node Info", scope=Scope.ONE)
    async def node_info(self, node: Node) -> Report: ...

``@action`` records only what a signature cannot say: the menu label and description, the
``scope``, whether the Action is ``destructive`` and so needs an explicit confirmation, and
the ``timeout_s`` bounding the whole invocation. The signature says the rest.
``scope=Scope.ONE`` means the Action acts on one Node, which the operator picks and which
arrives as the method's first argument; ``Scope.NONE`` means it acts on the Network and
takes no Node. Every remaining parameter is keyword-only and becomes a question in the
parameter form: a checkbox per ``bool`` and a number input per ``float``, which are the only
widget mappings there are.

An Action returns an ``ActionResult``, usually a ``Report``, describing *what happened*. It
must not emit platform markup: deciding what a result looks like belongs to the subclass,
and keeping that out of Actions is what allows the same Action to render anywhere.

``@prefill(some_action)`` marks a method as the source of that Action's starting form
values. It runs before the form opens and may talk to a Node, which is how the availability
form opens on a Node's real flags rather than on the signature's defaults. A prefill that
fails is logged and the defaults stand, since a form opening on defaults beats no form.

The flow
--------

A Chat Platform delivers each click as an independent event. Nothing links one click to the
previous one except the string written into the widget that was rendered last, so that
string carries the whole of the interaction state: a ``PendingInvocation`` recording which
Action, which Node, which parameters, and whether it has been confirmed.

``dispatch`` is therefore a pure function of that payload. It asks for whatever is still
missing, and runs the Action once nothing is::

    /whobot           PendingInvocation()             ->  the menu
    pick an Action    action="set_availability"       ->  the Node dropdown
    pick a Node       + node_url="http://..."         ->  the parameter form
    submit the form   + params={"chsh": True, ...}    ->  runs

Each widget carries a fuller payload than the one before, so the next click re-enters
``dispatch`` one step further along. Nothing about an interaction in progress is held in the
process, which means Whobot can restart between any two clicks and the next click still
works. It also means a payload can outlive the code that wrote it: an Action name that no
longer exists, or a Node that has left the registry, re-renders the step before it with a
note rather than raising or acting on the wrong Node.

``execute`` runs one Action as a tracked task rather than inline, because an Action takes
seconds to minutes while a Chat Platform expects to be acknowledged in about three. It
announces the run, awaits the Action under its ``timeout_s``, and posts the result. The
invariant that makes the bot trustworthy is that **every announcement is eventually followed
by a result**: a timeout, an unhandled exception and a shutdown that cancels the run each
produce a ``FAIL`` result rather than silence, because an announcement with no reply leaves
an operator unable to tell whether the work happened.

What a Chat Platform implements
-------------------------------

Six abstract methods: the four steps that can be asked for — ``show_menu``,
``ask_for_target``, ``ask_for_params``, ``ask_to_confirm`` — and the two halves of a run,
``announce_start`` and ``post_result``. A subclass implements those and nothing else. It
declares no Actions today, though the scan runs on every subclass and would find any it did.

This module must not reference a Chat Platform.
"""

import asyncio
import logging
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from typing import ClassVar

from pqn_node.api.routes.health import ComponentStatus
from pqn_node.api.routes.health import HealthStatus
from pqn_node.core.config import GamesAvailability
from pqn_whobot.actions import Action
from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import DigestResult
from pqn_whobot.actions import Field
from pqn_whobot.actions import NodeDigest
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import ReplyHandle
from pqn_whobot.actions import Report
from pqn_whobot.actions import Scope
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import action
from pqn_whobot.actions import prefill
from pqn_whobot.actions import scan_actions
from pqn_whobot.config import WhobotSettings
from pqn_whobot.node_client import NodeApiError
from pqn_whobot.node_client import NodeClient
from pqn_whobot.registry import UNKNOWN_NAME
from pqn_whobot.registry import Node
from pqn_whobot.registry import resolve_nodes

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_S = 10.0
"""How long a running Action gets to finish on shutdown before it is cancelled."""

SCREENSHOT_TIMEOUT_S = 60.0
"""Outer bound on one screenshot. The Node bounds the capture itself at 20s; the rest is
for a large PNG crossing the VPN."""

REBOOT_SETTLE_S = 15.0
"""How long to wait after a reboot is acknowledged before polling starts.

The Node answers and *then* begins shutting down, so a poll sent immediately reaches the API
that is about to die and reports a machine that never went away."""

REBOOT_POLL_INTERVAL_S = 5.0
"""How often a rebooting Node is asked whether it is back."""


def one_game_budget(settings: WhobotSettings) -> float:
    """How long an Action that plays one Game may take: the Game, plus the call that starts it."""
    return settings.node_timeout_s + settings.per_game_timeout_s


def whole_digest_budget(settings: WhobotSettings) -> float:
    """How long a whole-fleet digest may take: one Node's budget for every registered Node.

    This is why an Action's bound is a function of the settings rather than a constant. The
    digest's duration grows with the registry, so a constant would be wrong for every fleet but
    one — and when it fired, ``execute`` would post "Timed out" and throw away every section the
    run had already gathered. Adding a Node now widens this on its own.

    The extra Node call is for resolving the registry before any Node is checked.
    """
    return len(settings.nodes) * one_node_budget(settings) + settings.node_timeout_s


def one_node_budget(settings: WhobotSettings) -> float:
    """How long checking one Node over may take: two Node calls, then both Games it may play.

    Derived rather than configured. A ``per_node_timeout_s`` key would be a second statement of
    the same thing, free to disagree with it — and it did: the default was 900s while two Games
    at 600s each need 1200s, so a Node whose Games were merely slow lost both measurements to a
    budget nobody had chosen.
    """
    return 2 * settings.node_timeout_s + 2 * settings.per_game_timeout_s


DIGEST_TITLE = "Daily Digest"
"""What the fleet-wide report is called, whether it was scheduled or asked for by hand."""

BELL_CLASSICAL_LIMIT = 2.0
"""An S above this is the Bell inequality being violated, which is the point of the exercise."""

# FIXME: wrong home, and doing two jobs. What a Game is *called* is Node-domain knowledge —
#  `GamesAvailability` carries these names in comments — and `set_availability` names the same
#  three Games differently (`game.upper()`), so there are two namings in two places.
#  `_play_games` also iterates this as the list-of-Games, so display order silently decides
#  section order. Wants a home for Game domain facts, shared with `set_availability`.
GAME_TITLES = {
    "chsh": "CHSH — Verify Quantum Link",
    "qf": "Quantum Fortune",
    "ssm": "Share a Secret Message",
}
"""What each Game in ``GamesAvailability`` is called in front of an operator."""


class Whobot(ABC):
    """The platform-independent half of Whobot: its Actions and the flow that runs them.

    A Chat Platform subclasses this and implements the six abstract methods, which cover the
    menu, the target list, the parameter form, the confirmation, the start announcement and
    the result. Actions are found by scanning the class as it is created, so a subclass may
    declare Actions of its own; every Action shipped today is declared here.
    """

    actions: ClassVar[dict[str, Action]] = {}
    """Actions as the scan found them. Pure metadata, so one dict serves every instance."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Re-scan on every subclass, so a Chat Platform may add Actions of its own."""
        super().__init_subclass__(**kwargs)
        cls.actions = scan_actions(cls)

    def __init__(self, settings: WhobotSettings) -> None:
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()
        # Interruption replies, which must survive the cancellation that caused them.
        self._finalisers: set[asyncio.Task[None]] = set()
        self._accepting = True

    # ----------------------------------------------------------------------------------
    # Actions: what Whobot can do. Each returns an ActionResult.
    # ----------------------------------------------------------------------------------

    @action(label="List Nodes", description="Every Node in the registry, with its reachability.")
    async def list_nodes(self) -> Report:
        """Report every registered Node, whether Whobot can reach it, and its name."""
        nodes = await resolve_nodes(self.settings)
        if not nodes:
            return Report(
                status=Status.WARN,
                title="Nodes",
                summary="No Nodes are registered. Add a [[nodes]] entry to whobot.toml.",
            )

        fields = []
        for node in nodes:
            # Unreachable, reachable-but-not-as-expected, and reachable are three states, and
            # a row's glyph and its text have to agree about which one a Node is in.
            latency = "" if node.latency_ms is None else f" ({node.latency_ms:.0f}ms)"
            if not node.reachable:
                value, status = f"{node.api_url} — {node.error}", Status.FAIL
            elif node.warning:
                value, status = f"{node.api_url}{latency} — {node.warning}", Status.WARN
            else:
                value, status = f"{node.api_url}{latency}", Status.OK

            fields.append(Field(name=node.name, value=value, status=status))

        reachable = sum(1 for node in nodes if node.reachable)

        return Report(
            status=Status.overall(field.status for field in fields),
            title="Nodes",
            summary=f"{reachable} of {len(nodes)} reachable",
            sections=[Section(fields=fields)],
        )

    @action(label="Node Info", description="One Node's name and follower address.", scope=Scope.ONE)
    async def node_info(self, node: Node) -> Report:
        """Report what a Node says about itself, read fresh rather than from the registry."""
        try:
            config = await self._client(node).get_config(self.settings.node_timeout_s)
        except NodeApiError as e:
            return Report(
                status=Status.FAIL,
                title=f"{node.name} — {node.api_url}",
                summary="The Node did not answer.",
                sections=[Section(error=str(e))],
            )

        return Report(
            status=Status.OK if config.node_name else Status.WARN,
            title=f"{node.name} — {node.api_url}",
            summary=None
            if config.node_name
            else "This Node reports no name; it is running code from before that was added.",
            sections=[
                Section(
                    fields=[
                        Field(name="Name", value=config.node_name or UNKNOWN_NAME),
                        Field(name="Follower", value=config.follower_node_address or "none configured"),
                    ]
                )
            ],
        )

    @action(label="Change Game availability", description="Choose which Games a Node offers.", scope=Scope.ONE)
    async def set_availability(self, node: Node, *, chsh: bool = True, qf: bool = True, ssm: bool = True) -> Report:
        """Set which Games a Node offers, then report what the Node says afterwards.

        One parameter per Game, so the form asks one checkbox per Game. The report loop below
        reads ``GamesAvailability.model_fields``, so a Game added to the Node's model shows up
        there on its own — but it must be added to this signature too, or the form will never
        ask about it. ``test_the_form_asks_about_every_game`` is what makes that a failing test
        rather than a silent omission.

        The Node answers with *effective* availability, so a Game switched on here still
        reads as unavailable while the hardware it needs is unreachable. Reporting the
        request and the outcome side by side is the only way that difference is visible.
        """
        games = GamesAvailability(chsh=chsh, qf=qf, ssm=ssm)
        try:
            applied = await self._client(node).set_availability(games, self.settings.node_timeout_s)
        except NodeApiError as e:
            return Report(
                status=Status.FAIL,
                title=f"{node.name} — {node.api_url}",
                summary="The Node did not answer.",
                sections=[Section(error=str(e))],
            )

        fields = []
        for game in GamesAvailability.model_fields:
            wanted, is_on = getattr(games, game), getattr(applied, game)
            if wanted and not is_on:
                # The write did land in the Node's config; something on another machine is
                # holding the Game off, and it returns on its own once that is reachable. So
                # this must not read as "off, as you asked".
                value, status = "saved as on, but gated off by unreachable hardware", Status.WARN
            elif is_on and not wanted:
                # Not reachable today: gating only ever clears flags, and config is an
                # absolute veto. Reported rather than ignored, because silence would be a lie.
                value, status = "saved as off, but the Node still reports it on", Status.WARN
            elif is_on:
                value, status = "available", Status.ON
            else:
                value, status = "not available", Status.OFF

            fields.append(Field(name=game.upper(), value=value, status=status))

        gated = [field.name for field in fields if field.status is Status.WARN]
        return Report(
            status=Status.overall(field.status for field in fields),
            title=f"Game availability — {node.name}",
            summary=None
            if not gated
            else f"{', '.join(gated)}: enabled in config, but gated off. Check the Router and the follower Node.",
            sections=[Section(fields=fields)],
            notes=["Availability is saved to the Node's config and applied without a restart."],
        )

    @prefill(set_availability)
    async def _availability_prefill(self, node: Node) -> dict[str, object]:
        """Open the availability form on what the Node currently reports.

        ``model_dump`` keys this by Game name, which is what the form asks for, and is what
        keeps this method from naming the Games itself.
        """
        availability = await self._client(node).get_availability(self.settings.node_timeout_s)
        return dict(availability.model_dump())

    @action(
        label="Run CHSH",
        description="Measure one Node's quantum link now, at the angles you choose.",
        scope=Scope.ONE,
        timeout_s=one_game_budget,
    )
    async def run_chsh(self, node: Node, *, angle_a: float = 0.0, angle_b: float = 22.5) -> Report:
        """Run one CHSH measurement and report what it measured.

        The angles are asked for because they are a choice: an operator running this by hand is
        usually running it *at* something. The digest, which nobody is watching, takes them from
        configuration instead.
        """
        section = await self._play_chsh(node, (angle_a, angle_b))
        return Report(status=section.status or Status.OK, title=f"CHSH — {node.name}", sections=[section])

    @prefill(run_chsh)
    async def _chsh_angles_prefill(self, _node: Node) -> dict[str, object]:
        """Open the form on the angles an unattended run would use, so config is the starting point."""
        angle_a, angle_b = self.settings.basis
        return {"angle_a": angle_a, "angle_b": angle_b}

    @action(
        label="Run Quantum Fortune",
        description="Draw one number per channel from a Node's quantum randomness.",
        scope=Scope.ONE,
        timeout_s=one_game_budget,
    )
    async def run_fortune(self, node: Node) -> Report:
        """Run one Quantum Fortune and report what each channel drew.

        No parameters: ``fortune_size`` and ``channels`` are the Node's own calibration, and
        overriding them from Slack would make one run incomparable with the next.
        """
        section = await self._play_fortune(node)
        return Report(status=section.status or Status.OK, title=f"Quantum Fortune — {node.name}", sections=[section])

    @action(
        label="Run digest now",
        description="Check every Node in the registry, one after another.",
        timeout_s=whole_digest_budget,
    )
    async def run_digest(self) -> DigestResult:
        """Check every registered Node over, and report the fleet in one message.

        Nodes are checked **one at a time**, and that is a requirement rather than simplicity:
        in two-Node CHSH one Node acts as follower for another, so concurrent runs would contend
        for the same follower and the same timetagger, and the numbers would be worthless.
        """
        nodes = await resolve_nodes(self.settings)
        if not nodes:
            return DigestResult(
                status=Status.WARN,
                title=DIGEST_TITLE,
                summary="No Nodes are registered. Add a [[nodes]] entry to whobot.toml.",
            )

        budget = one_node_budget(self.settings)
        digests = []
        for node in nodes:
            try:
                digests.append(await asyncio.wait_for(self._probe_node(node), budget))
            except TimeoutError:
                # Bounded per Node so that one machine cannot spend the whole fleet's time. The
                # sections that Node had already produced are lost with it; what survives is
                # every *other* Node's, which is the point of the bound.
                logger.warning("%s took longer than its %ss budget", node.api_url, budget)
                digests.append(_timed_out(node, budget))

        healthy = sum(1 for digest in digests if digest.status is Status.OK)
        return DigestResult(
            status=Status.overall(digest.status for digest in digests),
            title=DIGEST_TITLE,
            summary=f"{healthy} of {len(digests)} Nodes reported no problems",
            nodes=digests,
        )

    @action(
        label="Check one Node",
        description="One Node's full check-up: its hardware, then the Games it offers.",
        scope=Scope.ONE,
        timeout_s=one_node_budget,
    )
    async def check_node(self, node: Node) -> DigestResult:
        """Check one Node the way the Daily Digest checks every Node.

        The same ``_probe_node`` the digest fans out over, so what an operator sees here is
        exactly what the unattended run would have reported about this machine.
        """
        digest = await self._probe_node(node)
        return DigestResult(
            status=digest.status,
            title=f"Check-up — {node.name}",
            summary=f"{node.api_url} — hardware and Games",
            nodes=[digest],
        )

    @action(
        label="Screenshot",
        description="A picture of what a Node's display is showing.",
        scope=Scope.ONE,
        timeout_s=SCREENSHOT_TIMEOUT_S,
    )
    async def screenshot(self, node: Node) -> Report:
        """Capture the Node's display and hand the image back for the reply to carry.

        This is the one thing no API probe can tell you: a Node whose every endpoint answers
        correctly can still be sitting in front of a crashed kiosk or a login screen.
        """
        if self.settings.debug_screenshot_path is not None:
            return self._debug_screenshot(node, self.settings.debug_screenshot_path)

        try:
            image = await self._client(node).get_screenshot(self.settings.node_timeout_s)
        except NodeApiError as e:
            return Report(
                status=Status.FAIL,
                title=f"Screenshot — {node.name}",
                summary="The Node did not return a screenshot.",
                sections=[Section(error=str(e))],
            )

        return Report(
            status=Status.OK,
            title=f"Screenshot — {node.name}",
            summary=f"{node.api_url} — {len(image) / 1024:,.0f} KB",
            image=image,
        )

    @staticmethod
    def _debug_screenshot(node: Node, path: Path) -> Report:
        """Answer with a file from disk instead of calling the Node.

        Screenshot is the one Action that cannot be exercised without a Node in front of a
        real display, so this exists to drive the whole path — the Action, the image on the
        result, the upload — from a laptop.

        It says so loudly. An image that is not of the Node is worse than no image at all if
        anyone mistakes it for one, so the report is a ``WARN`` naming the setting that
        produced it.
        """
        try:
            image = path.read_bytes()
        except OSError as e:
            return Report(
                status=Status.FAIL,
                title=f"Screenshot — {node.name}",
                summary="debug_screenshot_path is set in whobot.toml, and that file could not be read.",
                sections=[Section(error=str(e))],
            )

        return Report(
            status=Status.WARN,
            title=f"Screenshot — {node.name}",
            summary="This is not the Node's display. Whobot answered from a file and never called the Node.",
            image=image,
            notes=[f"debug_screenshot_path = {path}. Remove it from whobot.toml to screenshot the Node itself."],
        )

    @action(
        label="Reboot",
        description="Reboot a Node's host, then wait for its API to answer again.",
        scope=Scope.ONE,
        destructive=True,
        # The call that asks for the reboot, the wait for the machine, and the last poll of it.
        timeout_s=lambda s: s.node_timeout_s + s.reboot_wait_s + s.reachability_timeout_s,
    )
    async def reboot(self, node: Node) -> Report:
        """Reboot the Node's host and report whether it came back.

        Recovery is unattended: the machine autologs in, KDE autostart runs the Node's start
        script, and the API and kiosk come back on their own. The report is the point — an
        operator who asked for a reboot and got only "requested" has learnt nothing they
        could not have assumed, so this waits and says either how long it took or that it is
        still down.
        """
        title = f"Reboot — {node.name}"
        try:
            ack = await self._client(node).reboot(self.settings.node_timeout_s)
        except NodeApiError as e:
            return Report(
                status=Status.FAIL,
                title=title,
                summary="The Node did not accept the reboot, so nothing was rebooted.",
                sections=[Section(error=str(e))],
            )

        waited = self.settings.reboot_wait_s
        elapsed = await self._wait_until_back(node)
        if elapsed is None:
            return Report(
                status=Status.FAIL,
                title=title,
                summary=f"The Node has not answered in the {waited / 60:.0f} minutes since it was rebooted.",
                sections=[
                    Section(
                        fields=[
                            Field(name="Reboot", value=ack.detail or "scheduled", status=Status.OK),
                            Field(name="Node API", value=f"still down after {waited:.0f}s", status=Status.FAIL),
                        ]
                    )
                ],
                notes=["A reboot does not restart the Router or the Instrument Providers; those are another machine."],
            )

        return Report(
            status=Status.OK,
            title=title,
            summary=f"{node.api_url} is back, {elapsed:.0f}s after the reboot was requested.",
            sections=[
                Section(
                    fields=[
                        Field(name="Reboot", value=ack.detail or "scheduled", status=Status.OK),
                        Field(name="Node API", value=f"answering after {elapsed:.0f}s", status=Status.OK),
                    ]
                )
            ],
        )

    async def _wait_until_back(self, node: Node) -> float | None:
        """Poll a rebooting Node until it answers, returning how long that took.

        ``None`` means it never did within ``reboot_wait_s``, which is what the guardrail is
        actually for: the confirm step protects against rebooting the wrong Node, and this
        protects against one that does not return. How long to wait is configuration because
        it is site-specific; the settle period and the poll interval are not, because they
        describe how a host shuts down rather than how long an operator is willing to wait.

        Each poll is bounded by ``reachability_timeout_s`` rather than the Action's timeout,
        because "are you there?" is exactly the question, and a host that is off drops
        packets rather than refusing them — an unbounded poll would hang until the whole
        invocation timed out.
        """
        started = time.monotonic()
        await asyncio.sleep(REBOOT_SETTLE_S)
        client = self._client(node)

        while time.monotonic() - started < self.settings.reboot_wait_s:
            try:
                await client.get_config(self.settings.reachability_timeout_s)
            except NodeApiError:
                await asyncio.sleep(REBOOT_POLL_INTERVAL_S)
                continue
            return time.monotonic() - started

        return None

    # ----------------------------------------------------------------------------------
    # The flow. The single entry point from any Chat Platform.
    # ----------------------------------------------------------------------------------

    async def dispatch(self, pending: PendingInvocation, handle: ReplyHandle) -> None:
        """Ask for whatever is still missing, and run the Action once nothing is.

        A pure function of ``pending``: it never asks what happened before, so Whobot can
        restart between any two clicks and the next click still works.
        """
        if pending.action is None:
            return await self.show_menu(self._menu(), handle)

        act = self.actions.get(pending.action)
        if act is None:
            # The payload outlived the Action. Never a crash, and never a wrong Action.
            logger.info("payload names %r, which no longer exists", pending.action)
            return await self.show_menu(self._menu(), handle, note=f"{pending.action!r} no longer exists.")

        node: Node | None = None
        if act.scope is Scope.ONE:
            nodes = await resolve_nodes(self.settings)
            node = next((candidate for candidate in nodes if candidate.api_url == pending.node_url), None)
            if node is None:
                note = None if pending.node_url is None else f"{pending.node_url} is no longer registered."
                return await self.ask_for_target(act, nodes, handle, note=note)

        if act.parameters and pending.params is None:
            return await self.ask_for_params(act, pending, await self._initial_params(act, node), handle)

        if act.destructive and not pending.confirmed:
            return await self.ask_to_confirm(act, pending, handle)

        self._spawn(self.execute(act, pending, node, handle))
        return None

    async def execute(
        self,
        act: Action,
        pending: PendingInvocation,
        node: Node | None,
        handle: ReplyHandle,
    ) -> None:
        """Announce the run, run it, and post the outcome — whatever the outcome is.

        The invariant that makes the bot trustworthy is that **every announcement is
        eventually followed by a result**. An orphaned "Running Reboot on ufl-public-right"
        with no reply is worse than a clear failure: the operator cannot tell whether it
        happened, and has to go and check by hand. So every way out of the call posts
        something, including cancellation.
        """
        # Worked out before anything is announced, because it depends on configuration that a
        # long-running process may have had reloaded under it.
        timeout_s = act.timeout_for(self.settings)
        reply = await self.announce_start(act, pending, handle)

        try:
            result = await asyncio.wait_for(act.call(self, node, pending.params), timeout_s)
        except TimeoutError:
            logger.warning("%s timed out after %ss", act.name, timeout_s)
            result = Report(status=Status.FAIL, title=act.label, summary=f"Timed out after {timeout_s:.0f}s.")
        except asyncio.CancelledError:
            # Posting from inside a cancelled coroutine cannot be awaited here — the await
            # would be cancelled too. Hand it to a task nothing cancels, which shutdown
            # waits for, then let the cancellation continue.
            interrupted = Report(status=Status.FAIL, title=act.label, summary="Interrupted — Whobot shut down mid-run.")
            self._finalise(self.post_result(act, interrupted, reply))
            raise
        except Exception:
            # An Action may not take the process down, ever. That is what makes "kill a
            # Node mid-Action and the bot stays alive" true.
            logger.exception("%s raised", act.name)
            result = Report(status=Status.FAIL, title=act.label, summary="The Action raised an unhandled error.")

        await self.post_result(act, result, reply)

    # ----------------------------------------------------------------------------------
    # What a Chat Platform must provide. The whole abstract surface.
    # ----------------------------------------------------------------------------------

    @abstractmethod
    async def show_menu(self, actions: list[Action], handle: ReplyHandle, note: str | None = None) -> None: ...

    @abstractmethod
    async def ask_for_target(
        self, act: Action, nodes: list[Node], handle: ReplyHandle, note: str | None = None
    ) -> None: ...

    @abstractmethod
    async def ask_for_params(
        self, act: Action, pending: PendingInvocation, initial: dict[str, object], handle: ReplyHandle
    ) -> None: ...

    @abstractmethod
    async def ask_to_confirm(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> None: ...

    @abstractmethod
    async def announce_start(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> ReplyHandle: ...

    @abstractmethod
    async def post_result(self, act: Action, result: ActionResult, reply: ReplyHandle) -> None: ...

    # ----------------------------------------------------------------------------------
    # Running work, and stopping.
    # ----------------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run an Action without waiting for it, keeping a reference so it survives.

        Python garbage-collects a task nobody holds a reference to, so the set is
        load-bearing rather than bookkeeping.
        """
        if not self._accepting:
            coro.close()
            logger.warning("refusing new work: Whobot is shutting down")
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _finalise(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a reply that must outlive the cancellation that prompted it."""
        task = asyncio.create_task(coro)
        self._finalisers.add(task)
        task.add_done_callback(self._finalisers.discard)

    async def shutdown(self, grace_s: float = SHUTDOWN_GRACE_S) -> None:
        """Stop accepting work, let what is running finish, then cancel the rest.

        Each cancelled Action reports itself as interrupted, so shutdown waits for those
        replies too — otherwise the process would exit having orphaned exactly the
        announcements the invariant above promises to answer.
        """
        self._accepting = False

        if self._tasks:
            _, running = await asyncio.wait(set(self._tasks), timeout=grace_s)
            for task in running:
                task.cancel()
            if running:
                await asyncio.wait(running, timeout=grace_s)

        if self._finalisers:
            await asyncio.wait(set(self._finalisers), timeout=grace_s)

    # ----------------------------------------------------------------------------------
    # Helpers. None of these is an Action, so none can be invoked from a Chat Platform.
    # ----------------------------------------------------------------------------------

    def _client(self, node: Node) -> NodeClient:
        """Open a client for one Node. How long a call may take is stated at the call."""
        return NodeClient(node.api_url)

    def _menu(self) -> list[Action]:
        """Every Action, in the order they are declared. The menu is the class body."""
        return list(self.actions.values())

    # ----------------------------------------------------------------------------------
    # Checking one Node over. Shared by "Check one Node", the two Game Actions, and the
    # Daily Digest, which fans this out across the registry.
    # ----------------------------------------------------------------------------------

    async def _probe_node(self, node: Node) -> NodeDigest:
        """Check one Node's hardware, then the Games it offers.

        Every failure is a section rather than an exception, because a digest of four Nodes must
        not be lost to one of them being unplugged. Whoever calls this bounds it per Node.
        """
        if not node.reachable:
            # Already known from resolving the registry, so there is nothing to gain by spending
            # both Games' timeouts finding it out again.
            return NodeDigest(
                name=node.name,
                api_url=node.api_url,
                status=Status.FAIL,
                sections=[_failed_section("Node API", "Whobot cannot reach this Node.", node.error or "unreachable")],
            )

        try:
            sections = [_hardware_section(await self._client(node).get_health(self.settings.node_timeout_s))]
        except NodeApiError as e:
            sections = [_failed_section("Hardware", "The hardware probe failed.", str(e))]

        sections += await self._play_games(node)

        return NodeDigest(
            name=node.name,
            api_url=node.api_url,
            status=Status.overall(section.status for section in sections),
            sections=sections,
        )

    async def _play_games(self, node: Node) -> list[Section]:
        """Ask the Node which Games it offers, and play the ones it does.

        Availability that cannot be read skips every Game rather than assuming all of them are
        on. Whether a Game may run is the *Node's* answer to give: assuming is how an unattended
        run plays a Game an operator deliberately switched off.
        """
        try:
            availability = await self._client(node).get_availability(self.settings.node_timeout_s)
        except NodeApiError as e:
            reason = f"Whobot could not read this Node's Game availability: {e}"
            return [_skipped_section(title, reason) for title in GAME_TITLES.values()]

        sections = []
        if availability.chsh:
            sections.append(await self._play_chsh(node, self.settings.basis))
        else:
            sections.append(_skipped_section(GAME_TITLES["chsh"], "Not available on this Node."))

        if availability.qf:
            sections.append(await self._play_fortune(node))
        else:
            sections.append(_skipped_section(GAME_TITLES["qf"], "Not available on this Node."))

        # SSM is never played, whether it is available or not — hence no branch on availability.
        sections.append(
            _skipped_section(
                GAME_TITLES["ssm"], "Needs an interactive coordination dance, so an unattended run cannot play it."
            )
        )
        return sections

    async def _play_chsh(self, node: Node, basis: tuple[float, float]) -> Section:
        """Run one CHSH at these angles, and describe what it measured or why it could not."""
        label = GAME_TITLES["chsh"]
        started = time.monotonic()
        try:
            result = await self._client(node).run_chsh(
                self.settings.timetagger_address, basis, self.settings.per_game_timeout_s
            )
        except NodeApiError as e:
            return _failed_section(label, "The Game did not complete.", str(e), time.monotonic() - started)

        verdict = _bell_verdict(result.chsh_value)
        return Section(
            label=label,
            status=verdict.status,
            fields=[
                verdict,
                # Written out rather than dumped from the model: the error belongs beside the
                # value it qualifies, and "S" is what a physicist calls this.
                Field(name="S", value=f"{result.chsh_value:.4f} ± {result.chsh_error:.4f}"),
                Field(name="Basis", value=f"{basis[0]:.1f}°, {basis[1]:.1f}°"),
                Field(name="Expectation values", value=_angles(result.expectation_values)),
                Field(name="Sign-fixed expectations", value=_angles(result.expectation_values_sign_fixed)),
                Field(name="Expectation errors", value=_angles(result.expectation_errors)),
            ],
            note=f"{time.monotonic() - started:.1f}s",
        )

    async def _play_fortune(self, node: Node) -> Section:
        """Draw one Quantum Fortune, and describe what each channel got or why it could not."""
        label = GAME_TITLES["qf"]
        started = time.monotonic()
        try:
            drawn = await self._client(node).run_fortune(
                self.settings.timetagger_address, self.settings.per_game_timeout_s
            )
        except NodeApiError as e:
            return _failed_section(label, "The Game did not complete.", str(e), time.monotonic() - started)

        return Section(
            label=label,
            status=Status.OK,
            fields=[Field(name="Fortune per channel", value=", ".join(str(number) for number in drawn) or "nothing")],
            note=f"{time.monotonic() - started:.1f}s",
        )

    async def _initial_params(self, act: Action, node: Node | None) -> dict[str, object]:
        """Work out what a parameter form should open on.

        Signature defaults, unless the Action has a ``@prefill`` that can say better. A
        prefill talks to a Node and so can fail; a form opening on defaults is a great deal
        better than no form, so a failure is logged and the defaults stand.
        """
        defaults: dict[str, object] = {parameter.name: parameter.default for parameter in act.parameters}
        try:
            return defaults | await act.prefill_values(self, node)
        except Exception:
            logger.exception("%s: prefill failed, opening the form on its defaults", act.name)
            return defaults


# --------------------------------------------------------------------------------------
# Pure helpers for a Node's check-up. Free functions rather than methods, because nothing
# here needs a Node client — which keeps their tests the cheapest in the package.
# --------------------------------------------------------------------------------------


def _angles(values: list[float]) -> str:
    """Render a row of measured numbers, four decimals each, one convention for all of them."""
    return ", ".join(f"{value:.4f}" for value in values)


def _bell_verdict(chsh_value: float) -> Field:
    """State whether the Bell inequality was violated. The digest's one judgement.

    A ``WARN`` rather than a ``FAIL`` when it was not: the hardware answered and the Game ran,
    so nothing is broken in the sense the rest of the checklist means. What stopped is the
    demonstration of anything quantum, which is a different thing to go and look into.
    """
    if chsh_value > BELL_CLASSICAL_LIMIT:
        return Field(
            name="Bell inequality",
            value=f"violated, S = {chsh_value:.4f} is above the classical limit of {BELL_CLASSICAL_LIMIT}",
            status=Status.OK,
        )
    return Field(
        name="Bell inequality",
        value=f"not violated, S = {chsh_value:.4f} is within the classical limit of {BELL_CLASSICAL_LIMIT}",
        status=Status.WARN,
    )


def _component_field(label: str, component: ComponentStatus) -> Field:
    """Describe one probed thing as one checklist row, carrying its own status.

    Per-row and not per-section, because a Node with one dead device and nine live ones has to
    say which one is dead.
    """
    if component.reachable:
        value = "reachable" if component.latency_ms is None else f"reachable, {component.latency_ms:.0f}ms"
        return Field(name=label, value=value, status=Status.OK)
    return Field(name=label, value=component.error or "unreachable", status=Status.FAIL)


def _hardware_section(health: HealthStatus) -> Section:
    """Describe a Node's hardware, one row per probed thing."""
    fields = [_component_field("Router", health.router)]
    fields += [_component_field(f"{d.provider}/{d.name} ({d.purpose})", d) for d in health.devices]
    if health.rotary_encoder is None:
        # Not probed rather than broken: a Node with a virtual encoder has nothing to probe.
        fields.append(Field(name="Rotary encoder", value="virtual, not probed", status=Status.SKIPPED))
    else:
        fields.append(_component_field("Rotary encoder", health.rotary_encoder))
    if health.follower_node is not None:
        fields.append(_component_field("Follower Node", health.follower_node))

    return Section(label="Hardware", status=Status.overall(f.status for f in fields), fields=fields)


def _failed_section(label: str, summary: str, error: str, elapsed_s: float | None = None) -> Section:
    """Describe something that was attempted and did not work."""
    return Section(
        label=label,
        status=Status.FAIL,
        fields=[Field(name="Result", value=summary, status=Status.FAIL)],
        note=None if elapsed_s is None else f"{elapsed_s:.1f}s",
        error=error,
    )


def _timed_out(node: Node, budget_s: float) -> NodeDigest:
    """Describe a Node that outlasted the time the digest could give it."""
    return NodeDigest(
        name=node.name,
        api_url=node.api_url,
        status=Status.FAIL,
        sections=[
            _failed_section(
                "Check-up",
                f"This Node did not finish within the {budget_s:.0f}s a digest allows it.",
                "The run was cut off, so whatever it had already measured was lost with it.",
            )
        ],
    )


def _skipped_section(label: str, reason: str) -> Section:
    """Describe something that never ran.

    ``SKIPPED`` is not ``WARN``: one means it did not happen, the other that it did and looked
    wrong.
    """
    return Section(label=label, status=Status.SKIPPED, fields=[Field(name="Skipped", value=reason)])
