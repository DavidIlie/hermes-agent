"""Trusted Discord provenance for Fleet task creation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import reset_session_vars, set_session_vars
from tools import mcp_tool


def _run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        async def _install_lock_and_run():
            server = (
                mcp_tool._servers.get("friends_public_code")
                or mcp_tool._servers["fleet_tasks"]
            )
            if server._rpc_lock is None:
                server._rpc_lock = asyncio.Lock()
            return await coro

        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_session_context():
    reset_session_vars()
    yield
    reset_session_vars()


@pytest.fixture
def task_start_call():
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            isError=False,
            structuredContent=None,
        )
    )
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, {"fleet_tasks": server}), patch(
        "tools.mcp_tool._run_on_mcp_loop", side_effect=_run_on_mcp_loop
    ), patch.dict(mcp_tool._server_error_counts, {}, clear=True):
        yield session, mcp_tool._make_tool_handler(
            "fleet_tasks", "task_start", 30.0
        )


@pytest.fixture
def friend_task_call():
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            isError=False,
            structuredContent=None,
        )
    )
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with patch.dict(
        mcp_tool._servers, {"friends_public_code": server}
    ), patch(
        "tools.mcp_tool._run_on_mcp_loop", side_effect=_run_on_mcp_loop
    ), patch.dict(mcp_tool._server_error_counts, {}, clear=True):
        yield session, mcp_tool._make_tool_handler(
            "friends_public_code",
            "friend_task_request",
            30.0,
            inject_transport_provenance=True,
            require_transport_provenance=True,
        )


def _sent_arguments(session):
    return session.call_tool.await_args.kwargs["arguments"]


def test_discord_thread_overwrites_spoofed_provenance(task_start_call):
    session, handler = task_start_call
    runner = object.__new__(GatewayRunner)
    context = SessionContext(
        source=SessionSource(
            platform=Platform.DISCORD,
            scope_id="guild-1",
            chat_id="thread-3",
            parent_chat_id="channel-2",
            thread_id="thread-3",
            chat_type="thread",
            user_id="david-4",
        ),
        connected_platforms=[Platform.DISCORD],
        home_channels={},
        session_key="agent:main:discord:thread:thread-3",
    )
    tokens = runner._set_session_env(context)
    try:
        handler(
            {
                "request": "ship it",
                "_hermes_provenance": {
                    "platform": "discord",
                    "guild_id": "attacker-guild",
                    "user_id": "attacker",
                },
            }
        )
    finally:
        runner._clear_session_env(tokens)

    assert _sent_arguments(session) == {
        "request": "ship it",
        "_hermes_provenance": {
            "platform": "discord",
            "guild_id": "guild-1",
            "channel_id": "channel-2",
            "thread_id": "thread-3",
            "session_key": "agent:main:discord:thread:thread-3",
            "user_id": "david-4",
        },
    }


def test_discord_channel_uses_current_channel_without_thread(task_start_call):
    session, handler = task_start_call
    set_session_vars(
        platform="discord",
        scope_id="guild-1",
        chat_id="channel-2",
        session_key="agent:main:discord:group:channel-2",
        user_id="david-4",
    )

    handler({"request": "ship it"})

    assert _sent_arguments(session)["_hermes_provenance"] == {
        "platform": "discord",
        "guild_id": "guild-1",
        "channel_id": "channel-2",
        "session_key": "agent:main:discord:group:channel-2",
        "user_id": "david-4",
    }


def test_non_discord_session_drops_model_provenance(task_start_call):
    session, handler = task_start_call
    set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        session_key="agent:main:telegram:group:chat-1",
        user_id="user-2",
    )

    handler({"request": "ship it", "_hermes_provenance": {"platform": "discord"}})

    assert _sent_arguments(session) == {"request": "ship it"}


def test_unbound_environment_cannot_forge_discord_provenance(
    task_start_call, monkeypatch
):
    session, handler = task_start_call
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_SCOPE_ID", "spoofed-guild")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "spoofed-channel")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "spoofed-user")

    handler({"request": "ship it", "_hermes_provenance": {"user_id": "model"}})

    assert _sent_arguments(session) == {"request": "ship it"}


def test_incomplete_discord_context_drops_model_provenance(task_start_call):
    session, handler = task_start_call
    set_session_vars(
        platform="discord",
        scope_id="guild-1",
        chat_id="channel-2",
        session_key="agent:main:discord:group:channel-2",
        user_id="",
    )

    handler({"request": "ship it", "_hermes_provenance": {"user_id": "model"}})

    assert _sent_arguments(session) == {"request": "ship it"}


def _friend_context(**overrides):
    values = {
        "scope_id": "1537861134076350465",
        "chat_id": "1540773031436353708",
        "parent_chat_id": "1540773031436353707",
        "thread_id": "1540773031436353708",
        "user_id": "243009043260637184",
        "profile": "friends",
        "session_key": "agent:friends:discord:thread:1540773031436353708",
    }
    values.update(overrides)
    session_key = values.pop("session_key")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_type="thread",
        **values,
    )
    return SessionContext(
        source=source,
        connected_platforms=[Platform.DISCORD],
        home_channels={},
        session_key=session_key,
    )


def test_friend_profile_injects_complete_transport_provenance(friend_task_call):
    session, handler = friend_task_call
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(_friend_context())
    try:
        handler(
            {
                "repository": "public/example",
                "request": "fix it",
                "_hermes_provenance": {
                    "profile": "owner",
                    "guild_id": "attacker",
                    "user_id": "attacker",
                },
            }
        )
    finally:
        runner._clear_session_env(tokens)

    assert _sent_arguments(session) == {
        "repository": "public/example",
        "request": "fix it",
        "_hermes_provenance": {
            "platform": "discord",
            "profile": "friends",
            "guild_id": "1537861134076350465",
            "channel_id": "1540773031436353707",
            "thread_id": "1540773031436353708",
            "session_key": "agent:friends:discord:thread:1540773031436353708",
            "user_id": "243009043260637184",
        },
    }


@pytest.mark.parametrize(
    ("missing_field", "overrides"),
    [
        ("multiplex profile", {"profile": None}),
        ("guild", {"scope_id": None}),
        ("parent channel", {"parent_chat_id": None}),
        ("thread", {"thread_id": None}),
        ("requester", {"user_id": None}),
        ("session", {"session_key": ""}),
    ],
)
def test_required_friend_provenance_fails_closed_when_field_is_absent(
    friend_task_call, missing_field, overrides
):
    session, handler = friend_task_call
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(_friend_context(**overrides))
    try:
        result = json.loads(handler({"request": "fix it"}))
    finally:
        runner._clear_session_env(tokens)

    session.call_tool.assert_not_awaited()
    assert "error" in result
    assert missing_field in result["error"]
    assert "NOT called" in result["error"]


def test_required_friend_provenance_rejects_non_thread_route(friend_task_call):
    session, handler = friend_task_call
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(
        _friend_context(chat_id="1540773031436353999")
    )
    try:
        result = json.loads(handler({"request": "fix it"}))
    finally:
        runner._clear_session_env(tokens)

    session.call_tool.assert_not_awaited()
    assert "thread routing match" in result["error"]


def test_required_friend_provenance_fails_closed_when_context_read_breaks(
    friend_task_call,
):
    session, handler = friend_task_call

    with patch(
        "gateway.session_context.get_bound_session_env",
        side_effect=RuntimeError("broken context"),
    ):
        result = json.loads(handler({"request": "fix it"}))

    session.call_tool.assert_not_awaited()
    assert "context could not be read" in result["error"]
    assert "NOT called" in result["error"]


def test_reserved_provenance_is_hidden_from_model_schema():
    tool = SimpleNamespace(
        name="friend_task_request",
        description="Request work in a public repository",
        inputSchema={
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "_hermes_provenance": {"type": "object"},
            },
            "required": ["request", "_hermes_provenance"],
        },
    )

    assert mcp_tool._mcp_tool_accepts_transport_provenance(tool) is True
    assert mcp_tool._mcp_tool_requires_transport_provenance(tool) is True
    schema = mcp_tool._convert_mcp_schema("friends_public_code", tool)
    assert schema["parameters"]["properties"] == {
        "request": {"type": "string"}
    }
    assert schema["parameters"]["required"] == ["request"]
