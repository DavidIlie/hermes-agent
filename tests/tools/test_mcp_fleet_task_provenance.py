"""Trusted Discord provenance for Fleet task creation."""

import asyncio
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
            server = mcp_tool._servers["fleet_tasks"]
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
