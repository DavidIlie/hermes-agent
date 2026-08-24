"""Tests for Discord ignored_channels and no_thread_channels config."""

from types import SimpleNamespace
from datetime import datetime, timezone
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    """Install a mock discord module when discord.py isn't available."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeDMChannel:
    def __init__(self, channel_id: int = 1, name: str = "dm"):
        self.id = channel_id
        self.name = name


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int = 1,
        name: str = "general",
        guild_name: str = "Hermes Server",
        guild_id: int = 1540757211146493993,
    ):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name=guild_name, id=guild_id)
        self.topic = None


class FakeThread:
    def __init__(self, channel_id: int = 1, name: str = "thread", parent=None, guild_name: str = "Hermes Server"):
        self.id = channel_id
        self.name = name
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.guild = getattr(parent, "guild", None) or SimpleNamespace(name=guild_name)
        self.topic = None


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._text_batch_delay_seconds = 0  # disable batching for tests
    adapter.handle_message = AsyncMock()
    return adapter


def make_message(*, channel, content: str, mentions=None):
    author = SimpleNamespace(
        id=42,
        display_name="TestUser",
        name="TestUser",
        bot=False,
    )
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=list(mentions or []),
        attachments=[],
        reference=None,
        type=discord_platform.discord.MessageType.default,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=author,
        guild=getattr(channel, "guild", None),
    )


def test_multiplex_primary_admission_uses_routed_profile_authorization(
    adapter, monkeypatch,
):
    """A shared-token listener must not apply its owner's user allowlist first.

    The primary Discord adapter owns the one WebSocket connection, but the
    gateway route owns authorization. Otherwise an allow-all friends profile
    can never receive a message because the owner's adapter drops it before a
    SessionSource exists.
    """
    adapter._allowed_user_ids = {"243009043260637184"}
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _profile_name_for_source=lambda _source: "friends",
        _resolve_profile_home_for_source=lambda _source: "/profiles/friends",
        _is_user_authorized=lambda source, **_kwargs: source.profile == "friends",
    )
    adapter.gateway_runner = runner
    monkeypatch.setattr(
        "gateway.run._profile_runtime_scope",
        lambda _home: nullcontext(),
    )

    message = make_message(
        channel=FakeTextChannel(channel_id=1540773031436353708),
        content="hello herm",
    )

    admitted, role_authorized = adapter._discord_message_admission(
        message, claim=False,
    )

    assert admitted is True
    assert role_authorized is False


def test_multiplex_primary_admission_rejects_unrouted_shared_sender(
    adapter, monkeypatch,
):
    """Moving authorization behind routing must not open unrelated channels."""
    adapter._allowed_user_ids = {"243009043260637184"}
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _profile_name_for_source=lambda _source: None,
        _resolve_profile_home_for_source=lambda _source: "/profiles/default",
        _is_user_authorized=lambda _source, **_kwargs: False,
    )
    adapter.gateway_runner = runner
    monkeypatch.setattr(
        "gateway.run._profile_runtime_scope",
        lambda _home: nullcontext(),
    )

    message = make_message(
        channel=FakeTextChannel(channel_id=999999),
        content="<@999> try another channel",
        mentions=[adapter._client.user],
    )

    assert adapter._discord_message_admission(message, claim=False) == (
        False,
        False,
    )


def test_direct_message_is_rejected_before_profile_routing(adapter):
    """Channel-only mode drops DMs before routing, sessions, or model work."""
    adapter.config.extra["allow_dms"] = False
    adapter._allowed_user_ids = {"42"}
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _resolve_profile_home_for_source=MagicMock(
            side_effect=AssertionError("DM must not reach profile routing")
        ),
    )
    message = make_message(
        channel=FakeDMChannel(channel_id=1234),
        content="private message",
    )

    assert adapter._discord_message_admission(message, claim=False) == (
        False,
        False,
    )
    adapter.gateway_runner._resolve_profile_home_for_source.assert_not_called()


def test_direct_message_slash_command_is_rejected(adapter):
    """Slash commands cannot bypass channel-only mode through a DM."""
    adapter.config.extra["allow_dms"] = False
    interaction = SimpleNamespace(
        channel=FakeDMChannel(channel_id=1234),
        channel_id=1234,
        guild=None,
        guild_id=None,
        user=SimpleNamespace(id=42, name="TestUser"),
    )

    allowed, reason = adapter._evaluate_slash_authorization(
        interaction, "/status",
    )

    assert allowed is False
    assert reason == "Discord direct messages are disabled"


def test_yaml_channel_only_setting_seeds_adapter_config(monkeypatch):
    # Register the key with monkeypatch before the bridge mutates os.environ so
    # teardown removes the bridged value instead of leaking it to later tests.
    monkeypatch.setenv("DISCORD_ALLOW_DMS", "")

    extra = discord_platform._apply_yaml_config({}, {"allow_dms": False})

    assert extra["allow_dms"] == "false"


@pytest.mark.asyncio
async def test_free_response_channel_can_still_auto_thread_when_enabled(
    adapter, monkeypatch,
):
    """Mention-free channels can opt into thread-first conversations."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "789")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    adapter.config.extra["auto_thread_free_response_channels"] = True
    adapter._auto_create_thread = AsyncMock(
        return_value=FakeThread(
            channel_id=999,
            parent=FakeTextChannel(channel_id=789),
        )
    )

    message = make_message(
        channel=FakeTextChannel(channel_id=789),
        content="new thread without a mention",
    )
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.parent_chat_id == "789"


# ── ignored_channels ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ignored_channel_blocks_even_with_mention(adapter, monkeypatch):
    """Ignored channels take priority — even @mentions are dropped."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "500")

    bot_user = adapter._client.user
    message = make_message(
        channel=FakeTextChannel(channel_id=500),
        content=f"<@{bot_user.id}> hello",
        mentions=[bot_user],
    )
    await adapter._handle_message(message)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_ignored_channel_processes_normally(adapter, monkeypatch):
    """Channels not in the ignored list process normally."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "500,600")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    # Stub auto-thread creation so this test focuses on ignored-channel
    # routing only — auto-thread failures now correctly skip agent invocation
    # (#20243), which would otherwise mask the assertion below.
    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=700), content="hello")
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ignored_channels_empty_string_ignores_nothing(adapter, monkeypatch):
    """Empty DISCORD_IGNORED_CHANNELS means nothing is ignored."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    # Stub auto-thread creation so this test focuses on ignored-channel
    # routing only — auto-thread failures now correctly skip agent invocation
    # (#20243), which would otherwise mask the assertion below.
    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=500), content="hello")
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()


# ── no_thread_channels ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_thread_channel_skips_auto_thread(adapter, monkeypatch):
    """Channels in no_thread_channels should not auto-create threads."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_NO_THREAD_CHANNELS", "800")
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=800), content="hello")
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"


# ── auto-thread failure must not silently fall back to inline (#20243) ──


@pytest.mark.asyncio
async def test_auto_thread_failure_skips_agent_and_notifies_user(adapter, monkeypatch):
    """Auto-thread creation failure must not trigger an inline parent-channel reply.

    Before #20243, ``effective_channel = auto_threaded_channel or message.channel``
    silently routed the response back to the parent channel when thread creation
    failed, breaking thread-first Discord workflows. The fix surfaces a short
    visible error to the parent channel and skips agent invocation entirely so
    the user can retry.
    """
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=None)

    channel = FakeTextChannel(channel_id=800)
    channel.send = AsyncMock()
    message = make_message(channel=channel, content="hello")
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_awaited_once()
    # Agent must NOT be invoked when the routing target failed.
    adapter.handle_message.assert_not_awaited()
    # User gets a visible explanation in the parent channel instead of a silent
    # inline reply.
    channel.send.assert_awaited_once()
    sent_text = channel.send.await_args.args[0]
    assert "could not create" in sent_text.lower()
    assert "thread" in sent_text.lower()


# ── config.py bridging ───────────────────────────────────────────────


def test_config_bridges_ignored_channels(monkeypatch, tmp_path):
    """gateway/config.py bridges discord.ignored_channels to env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "discord": {
            "ignored_channels": ["111", "222"],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use setenv (not delenv) so monkeypatch registers cleanup even when
    # the var doesn't exist yet — load_gateway_config will overwrite it.
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("DISCORD_IGNORED_CHANNELS") == "111,222"
