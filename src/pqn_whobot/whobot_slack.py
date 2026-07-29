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
from pqn_whobot.actions import DigestResult
from pqn_whobot.actions import NodeDigest
from pqn_whobot.actions import Parameter
from pqn_whobot.actions import PayloadError
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import ReplyHandle
from pqn_whobot.actions import Report
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import decode
from pqn_whobot.actions import encode
from pqn_whobot.config import WhobotSettings
from pqn_whobot.config import config_path
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

BLOCK_LIMIT = 50
"""Slack's cap on the blocks in one message, and the only result that can reach it is the digest.

Its size grows with the fleet, and Slack answers an over-long message with a bare
``invalid_blocks`` — so without this the whole digest is lost rather than its tail. Four Nodes
fit comfortably; ten do not."""

NUMBER_TYPES: tuple[type, ...] = (float, int)
"""Parameter types rendered as a number input. An ``int`` disallows decimals; a ``float`` allows them."""

IMAGE_SUFFIXES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"GIF8", "gif"), (b"\xff\xd8\xff", "jpg"))
"""Magic numbers, so an upload can be named after what it actually is.

Slack decides how to display a file from its *filename*, so a GIF sent as ``.png`` arrives
broken. A screenshot is a PNG, but ``Report.image`` is only ``bytes``, and the debug
screenshot makes it whatever file an operator pointed the config at."""

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


def _param_block(name: str) -> str:
    """Name the block a single-parameter widget lives in; Slack echoes it back as the answer's key."""
    return f"whobot_param_{name}"


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


def _image_suffix(image: bytes) -> str:
    """Name an uploaded image after what its bytes say it is, defaulting to PNG."""
    return next((suffix for magic, suffix in IMAGE_SUFFIXES if image.startswith(magic)), "png")


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
            values = view.get("state", {}).get("values", {})
            await self._safely(replace(pending, params=self._read_form(act, values)), reply)

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
    def _read_form(act: Action, values: dict[str, Any]) -> dict[str, object]:
        """Read a submitted form back as one value per declared parameter.

        Slack reports only the *ticked* checkboxes, so an unticked one arrives as an absence
        rather than a ``False``. A submitted view echoes nothing else either — in particular
        **not** the ``initial_options`` it was rendered with — so the False floor has to come
        from what the Action declares. Without it a cleared box is missing from the payload,
        takes its default in ``coerce_params``, and switches the flag back *on*.

        A number input needs no floor: an empty one is genuinely "no answer", and leaving it out
        lets ``coerce_params`` supply the default.
        """
        params: dict[str, object] = {
            parameter.name: False for parameter in act.parameters if parameter.annotation is bool
        }
        for element in values.get(PARAMS_BLOCK, {}).values():
            for option in element.get("selected_options", []) or []:
                params[option["value"]] = True

        for parameter in act.parameters:
            if parameter.annotation not in NUMBER_TYPES:
                continue
            typed = (values.get(_param_block(parameter.name), {}).get(_param_block(parameter.name)) or {}).get("value")
            if typed:
                params[parameter.name] = typed
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
                "blocks": self._form_blocks(act, initial),
            },
        )

    @classmethod
    def _form_blocks(cls, act: Action, initial: dict[str, object]) -> list[Block]:
        """Generate the whole form from the Action's parameters.

        Every ``bool`` shares one checkbox group, because "which of these are on" is one
        question; every number gets an input of its own, since Slack keys a submitted value by
        the block it was in. The scan has already refused any parameter type without a mapping,
        so a parameter reaching here that is neither is a bug in the scan rather than a bad Action.
        """
        booleans = [p for p in act.parameters if p.annotation is bool]
        blocks = [cls._checkbox_block(booleans, initial)] if booleans else []
        blocks += [cls._number_block(p, initial) for p in act.parameters if p.annotation in NUMBER_TYPES]
        return blocks

    @staticmethod
    def _checkbox_block(parameters: list[Parameter], initial: dict[str, object]) -> Block:
        """Render the ``bool`` parameters as one checkbox group."""
        options = [_option(parameter.name.upper(), parameter.name) for parameter in parameters]
        ticked = [
            _option(parameter.name.upper(), parameter.name)
            for parameter in parameters
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

    @staticmethod
    def _number_block(parameter: Parameter, initial: dict[str, object]) -> Block:
        """Render one numeric parameter as a number input, opened on its starting value.

        Optional, so an operator who clears it gets the Action's default rather than a form
        that refuses to submit.
        """
        return {
            "type": "input",
            "block_id": _param_block(parameter.name),
            "optional": True,
            "label": {"type": "plain_text", "text": parameter.name.replace("_", " ").title()},
            "element": {
                "type": "number_input",
                "action_id": _param_block(parameter.name),
                "is_decimal_allowed": parameter.annotation is float,
                "initial_value": str(initial.get(parameter.name, parameter.default)),
            },
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

    def scheduled_handle(self) -> ReplyHandle:
        """Post to the digest channel, with no thread: an unattended digest is its own message."""
        return SlackReply(channel=self.settings.digest_channel)

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
                filename=f"{act.name}.{_image_suffix(image)}",
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
        blocks: list[Block] = [self._header(result.status, result.title)]
        if result.summary:
            blocks.append(_section(_escape(result.summary)))
        for section in result.sections:
            blocks += self._render_section(section)
        if result.notes:
            blocks.append(_context("\n".join(_escape(note) for note in result.notes)))
        return blocks

    @_render.register
    def _render_digest(self, result: DigestResult) -> list[Block]:
        """Render the digest: a divider and a Node line, then that Node's ordinary Sections.

        Everything about how a Section looks is inherited, so this adds only the grouping a flat
        result cannot express. A second header block per Node was tried and reads badly — Slack
        headers are all one size, so four Nodes look like four messages glued together.
        """
        blocks: list[Block] = [self._header(result.status, result.title)]
        if result.summary:
            blocks.append(_section(_escape(result.summary)))
        footer = [_context("\n".join(_escape(note) for note in result.notes))] if result.notes else []

        # One spare block for saying what was dropped, which must itself fit inside the limit.
        budget = BLOCK_LIMIT - len(footer) - 1
        for position, node in enumerate(result.nodes):
            rendered = self._render_node(node)
            if len(blocks) + len(rendered) > budget:
                omitted = [n.name for n in result.nodes[position:]]
                logger.warning("digest over Slack's %s-block limit; omitted %s", BLOCK_LIMIT, omitted)
                blocks.append(
                    _context(f"{len(omitted)} Nodes omitted, over Slack's message limit: {', '.join(omitted)}")
                )
                break
            blocks += rendered

        return blocks + footer

    def _render_node(self, node: NodeDigest) -> list[Block]:
        """One Node's part of the digest: a rule, a line naming it, and what was checked."""
        return [
            {"type": "divider"},
            _section(f"{self._emoji(node.status)} *{_escape(node.name)}* — {_escape(node.api_url)}"),
            *(block for section in node.sections for block in self._render_section(section)),
        ]

    @classmethod
    def _header(cls, status: Status, title: str) -> Block:
        """Slack truncates nothing itself: an over-long header is rejected, not shortened."""
        return {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{cls._emoji(status)} {title}"[:HEADER_LIMIT], "emoji": True},
        }

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

    async def check_digest_channel(self) -> str | None:
        """Return what is wrong with ``digest_channel``, or ``None`` if nothing is.

        The tokens' preflight exists because a bad one looks like a bot that started and never
        answered; a wrong channel is the same failure a day later — the digest simply doesn't
        arrive, and only the log says why. Checked at startup instead.

        Every answer but "the channel is there and Whobot is in it" stops the bot from starting,
        including a token that lacks the scope to look: an unverifiable channel is the state this
        exists to rule out. Only an empty ``digest_channel`` passes, since that turns the
        scheduled digest off deliberately.

        Reports rather than raises, so the CLI can say which setting is at fault instead of
        offering the guidance for a rejected token.
        """
        from slack_sdk.errors import SlackApiError  # noqa: PLC0415

        channel = self.settings.digest_channel
        if not channel:
            # A deliberate choice, not an error: start_scheduler already says the digest is off.
            return None

        try:
            info = await self.app.client.conversations_info(channel=channel)
        except SlackApiError as e:
            error = e.response.get("error", "unknown")
            if error == "missing_scope":
                # Refusing to start rather than warning: a check that can be skipped is a check
                # nobody has, and the alternative is the failure it exists to prevent. The
                # granted scopes are quoted back because `channels:read` and `channels:history`
                # sit next to each other in Slack's picker and only the first one works here.
                return (
                    f"Whobot cannot verify digest_channel {channel!r}: its bot token lacks channels:read "
                    "(groups:read for a private channel).\n"
                    f"  It currently has: {e.response.get('provided', 'nothing')}.\n"
                    "  Add the scope under OAuth & Permissions, then Reinstall to Workspace."
                )
            return (
                f"digest_channel {channel!r} in {config_path()} cannot be read: {error}. "
                "The channel ID is at the bottom of the channel's About tab in Slack."
            )

        if not info["channel"].get("is_member"):
            name = info["channel"].get("name", channel)
            return f"Whobot is not in #{name}, so it cannot post the Daily Digest there. Invite it to the channel."
        return None

    async def serve(self) -> None:
        """Hold the Socket Mode connection until the process is asked to stop.

        Bolt reconnects on its own. While disconnected an operator's click simply fails in
        their client — Slack does not queue interactions, so there is nothing to replay.
        """
        # Imported here so that importing this module needs no aiohttp, which keeps the
        # renderer tests independent of the transport.
        from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler  # noqa: PLC0415

        handler = AsyncSocketModeHandler(self.app, self.settings.slack_app_token)
        self.start_scheduler()
        try:
            # slack_bolt ships no annotations for these two, and mypy is strict here.
            await handler.start_async()  # type: ignore[no-untyped-call]
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("stopping")
        finally:
            await handler.close_async()  # type: ignore[no-untyped-call]
            await self.shutdown()
