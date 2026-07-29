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
parameter form: one checkbox per ``bool``, which is the only widget mapping there is.

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
from abc import ABC
from abc import abstractmethod
from collections.abc import Coroutine
from typing import Any
from typing import ClassVar

from pqn_node.core.config import GamesAvailability
from pqn_whobot.actions import Action
from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import Field
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
            config = await self._client(node).get_config()
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
            applied = await self._client(node).set_availability(games)
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
        availability = await self._client(node).get_availability()
        return dict(availability.model_dump())

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
        reply = await self.announce_start(act, pending, handle)

        try:
            result = await asyncio.wait_for(act.call(self, node, pending.params), act.timeout_s)
        except TimeoutError:
            logger.warning("%s timed out after %ss", act.name, act.timeout_s)
            result = Report(status=Status.FAIL, title=act.label, summary=f"Timed out after {act.timeout_s:.0f}s.")
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
        """Open a client for one Node, bounded by the timeout an Action's calls get."""
        return NodeClient(node.api_url, self.settings.node_timeout_s)

    def _menu(self) -> list[Action]:
        """Every Action, in the order they are declared. The menu is the class body."""
        return list(self.actions.values())

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
