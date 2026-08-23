#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <remote-fleet-image>" >&2
  exit 2
fi

image="$1"

docker run --rm --platform linux/amd64 \
  --entrypoint /opt/hermes/.venv/bin/python \
  "$image" -c '
from gateway.config import Platform
from gateway.profile_routing import ProfileRoute, match_profile_route
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.friend_code_approval import FriendCodeRelayConfig
from types import SimpleNamespace
import agent.secret_scope as secret_scope

routes = [
    ProfileRoute(
        name="friends",
        platform="discord",
        profile="friends",
        guild_id="guild",
        chat_id="channel",
    ),
    ProfileRoute(
        name="david",
        platform="discord",
        profile="friends-david",
        guild_id="guild",
        chat_id="channel",
        user_id="243009043260637184",
    ),
]
routes.sort(key=lambda route: route.specificity, reverse=True)

owner = match_profile_route(
    routes,
    "discord",
    guild_id="guild",
    chat_id="thread",
    parent_chat_id="channel",
    user_id="243009043260637184",
)
friend = match_profile_route(
    routes,
    "discord",
    guild_id="guild",
    chat_id="thread",
    parent_chat_id="channel",
    user_id="friend",
)

assert FriendCodeRelayConfig.__module__ == (
    "plugins.platforms.discord.friend_code_approval"
)
assert owner is not None and owner.profile == "friends-david"
assert friend is not None and friend.profile == "friends"

tokens = {
    "HERMES_FRIEND_CODE_DELIVERY_TOKEN": "delivery-token-for-image-smoke",
    "HERMES_FRIEND_CODE_OWNER_TOKEN": "owner-token-for-image-smoke",
}
secret_scope.get_secret = lambda name: tokens.get(name)
adapter = DiscordAdapter.__new__(DiscordAdapter)
adapter.platform = Platform.DISCORD
adapter.config = SimpleNamespace(
    extra={
        "friend_code_approval": {
            "enabled": True,
            "broker_url": "https://friend-broker.invalid",
            "owner_guild_id": "1111111111111111",
            "owner_channel_id": "2222222222222222",
            "owner_user_id": "243009043260637184",
            "friend_guild_id": "3333333333333333",
            "friend_channel_id": "4444444444444444",
        }
    }
)
adapter._configure_friend_code_relay()
assert adapter._friend_code_relay is not None
assert adapter._friend_code_state_store is not None
assert adapter._friend_code_bindings == {}
print("remote-fleet image imports and routing: ok")
'
