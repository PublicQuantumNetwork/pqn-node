"""Whobot on Slack: the only module that knows Block Kit or Bolt exists.

Everything here is rendering and transport. No Action lives in this file, and no decision
about *what* to report — only about what it looks like once decided.

Three Slack facts shape the code:

* **Bolt injects handler arguments by parameter name** (``ack``, ``body``, ``client``,
  ``logger``), so handlers are thin closures registered in ``_register_handlers`` rather
  than bound methods, whose ``self`` Bolt would try and fail to inject.
* **An interaction must be acked within 3 seconds**, which is Slack asking "did you receive
  this?" and not an answer. Every handler acks first and works afterwards.
* **The async flavour is a separate import path.** Mixing ``AsyncApp`` with the synchronous
  socket-mode handler yields a bot that connects and then silently never responds.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from functools import singledispatchmethod
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.webhook.async_client import AsyncWebhookClient

from pqn_whobot.actions import Action
from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import PayloadError
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import ReplyHandle
from pqn_whobot.actions import Report
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import decode
from pqn_whobot.actions import encode
from pqn_whobot.config import WhobotSettings
from pqn_whobot.registry import Node
from pqn_whobot.whobot import Whobot

logger = logging.getLogger(__name__)

Block = dict[str, Any]

OPTION_VALUE_LIMIT = 75
"""Slack's cap on a dropdown option's ``value``, and the tightest slot a payload rides in."""

HEADER_LIMIT = 150
"""Slack's cap on a header block's text."""

FIELDS_PER_SECTION = 10
"""Slack's cap on a section's ``fields`` grid. Actions emit one Section; this splits it."""

STATUS_EMOJI = {
    Status.OK: ":white_check_mark:",
    Status.WARN: ":warning:",
    Status.FAIL: ":x:",
    Status.SKIPPED: ":grey_question:",
    # The state pair. Dots rather than ticks and crosses, because these rows answer "is this
    # on" and a tick beside "not available" reads as a contradiction.
    Status.ON: ":large_green_circle:",
    Status.OFF: ":red_circle:",
}
"""One glyph per ``Status``. A test asserts the mapping is total, because ``_emoji`` falls
back to no glyph at all, which would silently drop the marker from every affected row."""

# Block IDs and action IDs. Slack sends these straight back, so they are the only way a
# handler knows which widget it is hearing from.
MENU_ACTION = "whobot_menu"
TARGET_ACTION = "whobot_target"
CONFIRM_ACTION = "whobot_confirm"
CANCEL_ACTION = "whobot_cancel"
PARAMS_VIEW = "whobot_params"
PARAMS_BLOCK = "whobot_params_block"


@dataclass(frozen=True)
class SlackReply(ReplyHandle):
    """Where a reply goes, and how. The base class never opens one of these.

    ``response_url`` addresses the ephemeral scaffolding — menu, target list, confirmation —
    each step replacing the last. ``channel`` and ``thread_ts`` address the public record
    that begins at ``announce_start``. ``trigger_id`` is Slack's permission to open a modal
    and is valid for about three seconds, which is why the form is opened from the handler's
    own turn rather than from a spawned task.
    """

    channel: str
    thread_ts: str | None = None
    response_url: str | None = None
    trigger_id: str | None = None
    replace: bool = False


def _escape(text: str) -> str:
    """Escape the three characters Slack treats as markup control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _target_label(node: Node) -> str:
    """Every rendered target shows name and address, resolved from the current registry."""
    return f"{node.name} — {node.api_url}"


def _option(text: str, value: str) -> Block:
    return {"text": {"type": "plain_text", "text": text[:75], "emoji": True}, "value": value}


def _option_value(pending: PendingInvocation, what: str) -> str:
    """Encode a payload for a dropdown option, refusing to build one Slack will reject.

    Slack answers an over-long option value with a bare ``invalid_blocks``, which names
    neither the block nor the field. Failing here instead means the message says which
    Action was being rendered.
    """
    encoded = encode(pending)
    if len(encoded) > OPTION_VALUE_LIMIT:
        msg = (
            f"{what}: encoded payload is {len(encoded)} characters, over Slack's "
            f"{OPTION_VALUE_LIMIT}-character option value limit: {encoded}"
        )
        raise ValueError(msg)
    return encoded


class WhobotSlack(Whobot):
    """Whobot speaking Slack. Declares no Actions; it only draws what it is asked to draw."""

    def __init__(self, settings: WhobotSettings) -> None:
        super().__init__(settings)
        self.app = AsyncApp(token=settings.slack_bot_token, raise_error_for_unhandled_request=False)
        self._register_handlers()

    # ----------------------------------------------------------------------------------
    # Bolt handlers. Each acks first, then hands the decoded payload to dispatch.
    # ----------------------------------------------------------------------------------

    def _register_handlers(self) -> None:
        """Register every handler as a closure, because Bolt injects arguments by name."""
        app = self.app

        @app.command("/whobot")
        async def _command(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            reply = SlackReply(
                channel=body.get("channel_id", ""),
                response_url=body.get("response_url"),
                trigger_id=body.get("trigger_id"),
            )
            await self._safely(PendingInvocation(), reply)

        @app.action(MENU_ACTION)
        async def _menu_chosen(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await self._from_interaction(body)

        @app.action(TARGET_ACTION)
        async def _target_chosen(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await self._from_interaction(body)

        @app.action(CONFIRM_ACTION)
        async def _confirmed(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await self._from_interaction(body)

        @app.action(CANCEL_ACTION)
        async def _cancelled(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await self._respond(self._reply_from(body, replace=True), [_section("Cancelled. Nothing was run.")])

        @app.view(PARAMS_VIEW)
        async def _form_submitted(ack: Any, body: dict[str, Any]) -> None:
            # A view submission carries no channel and no response_url, so both had to be
            # written into private_metadata when the modal was opened.
            await ack()
            view = body.get("view", {})
            try:
                metadata = json.loads(view.get("private_metadata") or "{}")
                pending = decode(metadata.get("pending", "{}"))
            except (PayloadError, ValueError):
                logger.exception("could not decode a modal's private_metadata")
                return
            reply = SlackReply(channel=metadata.get("channel", ""), response_url=metadata.get("response_url"))
            act = self.actions.get(pending.action or "")
            if act is None:
                # The modal outlived the Action it was rendered for. There is nothing to read
                # the form against, so let dispatch re-render with its note.
                await self._safely(replace(pending, params=None), reply)
                return
            values = view.get("state", {}).get("values", {}).get(PARAMS_BLOCK, {})
            await self._safely(replace(pending, params=self._read_checkboxes(act, values)), reply)

    async def _from_interaction(self, body: dict[str, Any]) -> None:
        """Decode the widget the operator just used and continue the flow."""
        actions = body.get("actions") or [{}]
        chosen = actions[0]
        raw = chosen.get("value") or (chosen.get("selected_option") or {}).get("value")
        reply = self._reply_from(body, replace=True)
        try:
            pending = decode(raw or "{}")
        except PayloadError:
            logger.exception("undecodable payload from Slack; re-rendering the menu")
            await self.show_menu(self._menu(), reply, note="That menu was not readable. Here it is again.")
            return
        await self._safely(pending, reply)

    @staticmethod
    def _reply_from(body: dict[str, Any], *, replace: bool = False) -> SlackReply:
        return SlackReply(
            channel=(body.get("channel") or {}).get("id", ""),
            response_url=body.get("response_url"),
            trigger_id=body.get("trigger_id"),
            replace=replace,
        )

    @staticmethod
    def _read_checkboxes(act: Action, values: dict[str, Any]) -> dict[str, object]:
        """Read a checkbox group back as one boolean per declared parameter.

        Slack reports only the *ticked* boxes, so an unticked one arrives as an absence
        rather than a ``False``. A submitted view echoes nothing else either — in particular
        **not** the ``initial_options`` it was rendered with — so the False floor has to come
        from what the Action declares. Without it a cleared box is missing from the payload,
        takes its default in ``coerce_params``, and switches the flag back *on*.
        """
        params: dict[str, object] = {
            parameter.name: False for parameter in act.parameters if parameter.annotation is bool
        }
        for element in values.values():
            for option in element.get("selected_options", []) or []:
                params[option["value"]] = True
        return params

    async def _safely(self, pending: PendingInvocation, reply: SlackReply) -> None:
        """Run dispatch so that no handler can take the socket down.

        Bolt logs and swallows a handler exception, but a Whobot that stops answering is
        worse than one that says it failed, so the operator is told either way.
        """
        try:
            await self.dispatch(pending, reply)
        except Exception:
            logger.exception("dispatch failed for %r", pending)
            await self._respond(reply, [_section(":x: Whobot could not handle that. Check its logs.")])

    # ----------------------------------------------------------------------------------
    # The ephemeral scaffolding: each step replaces the last.
    # ----------------------------------------------------------------------------------

    async def show_menu(self, actions: list[Action], handle: ReplyHandle, note: str | None = None) -> None:
        reply = self._slack(handle)
        options = [
            _option(act.label, _option_value(PendingInvocation(action=act.name), f"menu entry {act.name!r}"))
            for act in actions
        ]
        blocks: list[Block] = []
        if note:
            blocks.append(_context(f":warning: {_escape(note)}"))
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*What would you like Whobot to do?*"},
                "accessory": {
                    "type": "static_select",
                    "action_id": MENU_ACTION,
                    "placeholder": {"type": "plain_text", "text": "Choose an Action"},
                    "options": options,
                },
            }
        )
        descriptions = [f"*{_escape(a.label)}* — {_escape(a.description)}" for a in actions if a.description]
        if descriptions:
            blocks.append(_context("\n".join(descriptions)))
        await self._respond(reply, blocks)

    async def ask_for_target(
        self, act: Action, nodes: list[Node], handle: ReplyHandle, note: str | None = None
    ) -> None:
        reply = self._slack(handle)
        if not nodes:
            await self._respond(reply, [_section("No Nodes are registered. Add one to `whobot.toml`.")])
            return

        options = [
            _option(
                _target_label(node),
                _option_value(PendingInvocation(action=act.name, node_url=node.api_url), f"target for {act.name!r}"),
            )
            for node in nodes
        ]
        blocks: list[Block] = []
        if note:
            blocks.append(_context(f":warning: {_escape(note)}"))
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{_escape(act.label)}* — which Node?"},
                "accessory": {
                    "type": "static_select",
                    "action_id": TARGET_ACTION,
                    "placeholder": {"type": "plain_text", "text": "Choose a Node"},
                    "options": options,
                },
            }
        )
        await self._respond(reply, blocks)

    async def ask_for_params(
        self, act: Action, pending: PendingInvocation, initial: dict[str, object], handle: ReplyHandle
    ) -> None:
        """Open a modal generated from the Action's signature, on the values given."""
        reply = self._slack(handle)
        if reply.trigger_id is None:
            logger.error("%s: no trigger_id, so no modal can be opened", act.name)
            await self._respond(reply, [_section(":x: Slack did not allow a form to open. Try again.")])
            return

        metadata = json.dumps(
            {"pending": encode(pending), "channel": reply.channel, "response_url": reply.response_url}
        )
        await self.app.client.views_open(
            trigger_id=reply.trigger_id,
            view={
                "type": "modal",
                "callback_id": PARAMS_VIEW,
                "title": {"type": "plain_text", "text": act.label[:24]},
                "submit": {"type": "plain_text", "text": "Run"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "private_metadata": metadata,
                "blocks": [self._checkbox_block(act, initial)],
            },
        )

    @staticmethod
    def _checkbox_block(act: Action, initial: dict[str, object]) -> Block:
        """Render every parameter as a checkbox. The only widget mapping that exists.

        The scan has already refused any parameter type without a mapping, so reaching here
        with something other than a ``bool`` is a bug in the scan rather than a bad Action.
        """
        options = [_option(parameter.name.upper(), parameter.name) for parameter in act.parameters]
        ticked = [
            _option(parameter.name.upper(), parameter.name)
            for parameter in act.parameters
            if initial.get(parameter.name, parameter.default)
        ]
        element: Block = {"type": "checkboxes", "action_id": PARAMS_BLOCK, "options": options}
        if ticked:
            # Slack rejects an empty initial_options outright, so it is omitted rather than sent.
            element["initial_options"] = ticked
        return {
            "type": "input",
            "block_id": PARAMS_BLOCK,
            "optional": True,
            "label": {"type": "plain_text", "text": "Enabled"},
            "element": element,
        }

    async def ask_to_confirm(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> None:
        """Name the target before anything happens. A dropdown pick must never act."""
        reply = self._slack(handle)
        target = f" on `{_escape(pending.node_url)}`" if pending.node_url else ""
        await self._respond(
            reply,
            [
                _section(f":warning: *{_escape(act.label)}*{target}.\nThis cannot be undone. Run it?"),
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": CONFIRM_ACTION,
                            "style": "danger",
                            "text": {"type": "plain_text", "text": f"Yes, {act.label}"},
                            # A button's value allows 2000 characters, so params ride here.
                            "value": encode(replace(pending, confirmed=True)),
                        },
                        {
                            "type": "button",
                            "action_id": CANCEL_ACTION,
                            "text": {"type": "plain_text", "text": "Cancel"},
                            "value": "{}",
                        },
                    ],
                },
            ],
        )

    # ----------------------------------------------------------------------------------
    # The public record: an in-channel ack, then the result threaded under it.
    # ----------------------------------------------------------------------------------

    async def announce_start(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> ReplyHandle:
        """Say publicly that work has begun, and return where its result belongs.

        Everything before this is scaffolding nobody else needs to watch. The moment work
        starts it becomes record — which matters, because with no Slack-side access control
        the channel *is* the audit log of who changed what on which Node.
        """
        reply = self._slack(handle)
        channel = reply.channel or self.settings.digest_channel
        target = f" on `{_escape(pending.node_url)}`" if pending.node_url else ""
        posted = await self.app.client.chat_postMessage(
            channel=channel,
            text=f"Running {act.label}…",
            blocks=[_section(f":hourglass_flowing_sand: Running *{_escape(act.label)}*{target}…")],
        )
        return SlackReply(channel=channel, thread_ts=posted["ts"])

    async def post_result(self, act: Action, result: ActionResult, reply: ReplyHandle) -> None:
        """Post an Action's outcome as a threaded reply under its announcement."""
        slack = self._slack(reply)
        blocks = self._render(result)
        await self.app.client.chat_postMessage(
            channel=slack.channel,
            thread_ts=slack.thread_ts,
            text=act.label,
            blocks=blocks,
        )
        image = getattr(result, "image", None)
        if image:
            await self.app.client.files_upload_v2(
                channel=slack.channel,
                thread_ts=slack.thread_ts,
                file=image,
                filename=f"{act.name}.png",
                title=act.label,
            )

    # ----------------------------------------------------------------------------------
    # Renderers: pure ActionResult -> blocks, so they can be tested without posting.
    # ----------------------------------------------------------------------------------

    @singledispatchmethod
    def _render(self, result: ActionResult) -> list[Block]:
        """Refuse to guess. A result type with no renderer is caught by a test, not here."""
        msg = f"no renderer registered for {type(result).__name__}"
        raise NotImplementedError(msg)

    @_render.register
    def _render_report(self, result: Report) -> list[Block]:
        blocks: list[Block] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{self._emoji(result.status)} {result.title}"[:HEADER_LIMIT],
                    "emoji": True,
                },
            }
        ]
        if result.summary:
            blocks.append(_section(_escape(result.summary)))
        for section in result.sections:
            blocks += self._render_section(section)
        if result.notes:
            blocks.append(_context("\n".join(_escape(note) for note in result.notes)))
        return blocks

    @classmethod
    def _render_section(cls, section: Section) -> list[Block]:
        """Render one Section, splitting its measurements across Slack's ten-field cap."""
        blocks: list[Block] = []
        if section.label:
            heading = f"*{_escape(section.label)}*"
            if section.status is not None:
                heading = f"{cls._emoji(section.status)} {heading}"
            blocks.append(_section(heading))

        # A Field with a status is a checklist entry and renders as a line; one without is a
        # measurement and renders in a grid. The rule comes from the data, not from a flag.
        lines = [f"{cls._emoji(f.status)} {_escape(f.name)} — {_escape(f.value)}" for f in section.fields if f.status]
        if lines:
            blocks.append(_section("\n".join(lines)))

        grid = [f for f in section.fields if f.status is None]
        blocks.extend(
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": f"*{_escape(f.name)}*\n{_escape(f.value)}"} for f in chunk],
            }
            for chunk in _chunked(grid, FIELDS_PER_SECTION)
        )

        if section.note:
            blocks.append(_context(_escape(section.note)))
        if section.error:
            blocks.append(_section(f"```{section.error[:2800]}```"))
        return blocks

    @staticmethod
    def _emoji(status: Status | None) -> str:
        return STATUS_EMOJI.get(status, "") if status is not None else ""

    # ----------------------------------------------------------------------------------
    # Plumbing.
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _slack(handle: ReplyHandle) -> SlackReply:
        """Narrow the opaque handle the base class passes around back to this platform's."""
        if not isinstance(handle, SlackReply):
            msg = f"WhobotSlack was handed a {type(handle).__name__}, not a SlackReply"
            raise TypeError(msg)
        return handle

    @staticmethod
    async def _respond(reply: SlackReply, blocks: list[Block]) -> None:
        """Send an ephemeral step, replacing the previous one where there is one."""
        if reply.response_url is None:
            logger.error("no response_url; dropping an ephemeral message")
            return
        await AsyncWebhookClient(reply.response_url).send(
            text="Whobot",
            blocks=blocks,
            response_type="ephemeral",
            replace_original=reply.replace,
        )

    async def check_credentials(self) -> None:
        """Verify both tokens before connecting, raising ``SlackApiError`` if either is bad.

        This exists because ``start_async`` **retries forever** rather than raising: with a
        rejected token an operator sees a bot that appears to start, logs a traceback every
        few seconds, and never answers. Retrying is right for a network that dropped and
        wrong for a credential that will never be accepted, and only a preflight can tell
        those apart. Each token is checked by the cheapest call that exercises it.
        """
        from slack_sdk.web.async_client import AsyncWebClient  # noqa: PLC0415

        await self.app.client.auth_test()
        # The URL this returns is deliberately discarded; the handler opens its own.
        await AsyncWebClient().apps_connections_open(app_token=self.settings.slack_app_token)

    async def serve(self) -> None:
        """Hold the Socket Mode connection until the process is asked to stop.

        Bolt reconnects on its own. While disconnected an operator's click simply fails in
        their client — Slack does not queue interactions, so there is nothing to replay.
        """
        # Imported here so that importing this module needs no aiohttp, which keeps the
        # renderer tests independent of the transport.
        from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler  # noqa: PLC0415

        handler = AsyncSocketModeHandler(self.app, self.settings.slack_app_token)
        try:
            # slack_bolt ships no annotations for these two, and mypy is strict here.
            await handler.start_async()  # type: ignore[no-untyped-call]
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("stopping")
        finally:
            await handler.close_async()  # type: ignore[no-untyped-call]
            await self.shutdown()
