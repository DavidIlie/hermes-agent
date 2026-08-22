"""Security and recovery tests for the non-model friend approval relay."""

import json
import stat
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, FriendCodeApprovalView
from plugins.platforms.discord.friend_code_approval import (
    FriendCodeBinding,
    FriendCodeRelayConfig,
    FriendCodeRelayError,
    FriendCodeStateStore,
    friend_status_content,
    validate_decision_receipt,
    validate_status_receipt,
)


OWNER_GUILD = "853884362436116491"
OWNER_CHANNEL = "1537861134076350465"
OWNER_USER = "243009043260637184"
FRIEND_GUILD = "1540757211146493993"
FRIEND_CHANNEL = "1540773031436353708"
FRIEND_THREAD = "153700000000000003"
FRIEND_USER = "153700000000000004"
OWNER_MESSAGE = "1540773031436353710"
TOKEN = "owner-token-with-enough-entropy"


def _config() -> FriendCodeRelayConfig:
    return FriendCodeRelayConfig.from_mapping({
        "broker_url": "http://hermes-broker.hermes.svc.cluster.local:8080",
        "owner_guild_id": OWNER_GUILD,
        "owner_channel_id": OWNER_CHANNEL,
        "owner_user_id": OWNER_USER,
        "friend_guild_id": FRIEND_GUILD,
        "friend_channel_id": FRIEND_CHANNEL,
    })


def _notification() -> dict:
    return {
        "id": 41,
        "kind": "approval_requested",
        "created_at": "2026-08-22T10:00:00Z",
        "request": {
            "id": "friend_0123456789abcdef01234567",
            "version": 3,
            "request_hash": "a" * 64,
            "kind": "existing_public",
            "request": "Fix the public README and open a pull request.",
            "repository": {
                "id": 123,
                "full_name": "DavidIlie/public-demo",
                "url": "https://github.com/DavidIlie/public-demo",
                "default_branch": "main",
                "archived": False,
                "disabled": False,
                "fork": False,
            },
            "new_repository_name": "",
            "origin": {
                "platform": "discord",
                "profile": "friends",
                "guild_id": FRIEND_GUILD,
                "channel_id": FRIEND_CHANNEL,
                "thread_id": FRIEND_THREAD,
                "user_id": FRIEND_USER,
                "session_hash": "b" * 64,
            },
            "state": "awaiting_owner_approval",
            "execution_ready": False,
            "expires_at": "2026-08-23T10:00:00Z",
        },
    }


def _binding() -> FriendCodeBinding:
    binding = FriendCodeBinding.from_notification(_notification(), _config())
    binding.message_id = OWNER_MESSAGE
    return binding


def test_notification_is_fixed_to_friend_boundary_and_public_repo():
    binding = _binding()

    assert binding.owner_user_id == OWNER_USER
    assert binding.friend_thread_id == FRIEND_THREAD
    assert binding.repository["full_name"] == "DavidIlie/public-demo"
    assert binding.state == "awaiting_owner_approval"

    private = _notification()
    private["request"]["repository"]["url"] = "https://evil.example/private"
    with pytest.raises(FriendCodeRelayError):
        FriendCodeBinding.from_notification(private, _config())

    owner_origin = _notification()
    owner_origin["request"]["origin"]["user_id"] = OWNER_USER
    with pytest.raises(FriendCodeRelayError):
        FriendCodeBinding.from_notification(owner_origin, _config())


def test_button_id_authenticates_decision_message_and_request_version():
    binding = _binding()
    custom_id = binding.button_custom_id("approve", TOKEN)

    assert binding.verify_custom_id(custom_id, "approve", TOKEN)
    assert not binding.verify_custom_id(custom_id, "deny", TOKEN)
    assert not binding.verify_custom_id(custom_id, "approve", TOKEN + "-wrong")

    altered = deepcopy(binding)
    altered.version += 1
    assert not altered.verify_custom_id(custom_id, "approve", TOKEN)
    altered = deepcopy(binding)
    altered.message_id = str(int(OWNER_MESSAGE) + 1)
    assert not altered.verify_custom_id(custom_id, "approve", TOKEN)


def test_state_store_is_owner_only_and_fails_closed_on_boundary_change(tmp_path):
    path = tmp_path / "gateway" / "friend_code_approvals.json"
    binding = _binding()
    store = FriendCodeStateStore(path, _config())
    store.save({binding.event_id: binding})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load()[binding.event_id].message_id == OWNER_MESSAGE

    payload = json.loads(path.read_text())
    payload["bindings"][binding.event_id]["owner_user_id"] = FRIEND_USER
    path.write_text(json.dumps(payload))
    with pytest.raises(FriendCodeRelayError):
        store.load()


def test_receipts_are_exact_and_friend_status_has_no_owner_metadata():
    binding = _binding()
    decision = {
        "request": binding.request_id,
        "version": binding.version,
        "hash": binding.request_hash,
        "decision": "approve",
        "state": "approved_blocked",
        "execution_ready": False,
    }
    assert validate_decision_receipt(decision, binding, "approve") == "approved_blocked"

    mismatch = dict(decision, hash="c" * 64)
    with pytest.raises(FriendCodeRelayError):
        validate_decision_receipt(mismatch, binding, "approve")

    status = {
        "event": binding.event_id,
        "request": binding.request_id,
        "version": binding.version,
        "hash": binding.request_hash,
        "state": "expired",
        "execution_ready": False,
        "friend_origin": {
            "guild_id": FRIEND_GUILD,
            "channel_id": FRIEND_CHANNEL,
            "thread_id": FRIEND_THREAD,
            "user_id": FRIEND_USER,
        },
    }
    assert validate_status_receipt(status, binding, _config()) == "expired"
    content = friend_status_content(binding, "expired")
    assert OWNER_USER not in content
    assert OWNER_GUILD not in content
    assert OWNER_CHANNEL not in content


@pytest.mark.asyncio
async def test_persistent_view_forwards_actual_discord_custom_id():
    adapter = SimpleNamespace(_handle_friend_code_decision=AsyncMock())
    view = FriendCodeApprovalView(
        adapter=adapter,
        event_id="41",
        approve_custom_id="hfc1:a:expected",
        deny_custom_id="hfc1:d:expected",
    )
    interaction = SimpleNamespace(data={"custom_id": "hfc1:a:from-discord"})

    assert view.timeout is None
    await view.children[0].callback(interaction)

    adapter._handle_friend_code_decision.assert_awaited_once_with(
        interaction,
        event_id="41",
        decision="approve",
        custom_id="hfc1:a:from-discord",
    )


class _Response:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


def _interaction(*, user=OWNER_USER, custom_id=""):
    return SimpleNamespace(
        user=SimpleNamespace(id=int(user)),
        guild=SimpleNamespace(id=int(OWNER_GUILD)),
        channel_id=int(OWNER_CHANNEL),
        message=SimpleNamespace(id=int(OWNER_MESSAGE)),
        data={"custom_id": custom_id},
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_only_exact_owner_can_decide_and_timeout_recovers_by_idempotency():
    adapter = DiscordAdapter(PlatformConfig())
    binding = _binding()
    receipt = {
        "request": binding.request_id,
        "version": binding.version,
        "hash": binding.request_hash,
        "decision": "approve",
        "state": "approved_blocked",
        "execution_ready": False,
    }
    relay = SimpleNamespace(
        owner_token=TOKEN,
        decide=AsyncMock(side_effect=TimeoutError()),
        decision_receipt=AsyncMock(return_value=receipt),
    )
    adapter._friend_code_relay = relay
    adapter._friend_code_bindings = {binding.event_id: binding}
    adapter._friend_code_state_store = SimpleNamespace(save=lambda _: None)
    adapter._ensure_friend_code_owner_card = AsyncMock()
    adapter._ensure_friend_code_status_delivery = AsyncMock()
    custom_id = binding.button_custom_id("approve", TOKEN)

    intruder = _interaction(user=FRIEND_USER, custom_id=custom_id)
    await adapter._handle_friend_code_decision(
        intruder, event_id=binding.event_id, decision="approve", custom_id=custom_id
    )
    relay.decide.assert_not_awaited()
    intruder.response.send_message.assert_awaited_once()

    owner = _interaction(custom_id=custom_id)
    await adapter._handle_friend_code_decision(
        owner, event_id=binding.event_id, decision="approve", custom_id=custom_id
    )

    relay.decide.assert_awaited_once_with(
        binding,
        "approve",
        clicker_guild_id=OWNER_GUILD,
        clicker_channel_id=OWNER_CHANNEL,
        clicker_user_id=OWNER_USER,
    )
    relay.decision_receipt.assert_awaited_once_with(binding, "approve")
    assert binding.state == "approved_blocked"
    adapter._ensure_friend_code_owner_card.assert_awaited_once_with(binding)
    adapter._ensure_friend_code_status_delivery.assert_awaited_once_with(binding)


@pytest.mark.asyncio
async def test_owner_card_recovery_uses_marker_instead_of_duplicate_send():
    adapter = DiscordAdapter(PlatformConfig())
    binding = _binding()
    binding.message_id = ""
    relay = SimpleNamespace(owner_token=TOKEN)
    message = SimpleNamespace(
        id=int(OWNER_MESSAGE),
        content="prefix " + binding.marker,
        edit=AsyncMock(),
    )

    async def history(**_):
        yield message

    channel = SimpleNamespace(
        guild=SimpleNamespace(id=int(OWNER_GUILD)),
        history=history,
        send=AsyncMock(),
    )
    client = SimpleNamespace(
        get_channel=lambda _: channel,
        fetch_channel=AsyncMock(),
        add_view=lambda *_args, **_kwargs: None,
    )
    adapter._client = client
    adapter._friend_code_relay = relay
    adapter._friend_code_bindings = {binding.event_id: binding}
    adapter._friend_code_state_store = SimpleNamespace(save=lambda _: None)

    await adapter._ensure_friend_code_owner_card(binding)

    channel.send.assert_not_awaited()
    assert binding.message_id == OWNER_MESSAGE
    message.edit.assert_awaited_once()
