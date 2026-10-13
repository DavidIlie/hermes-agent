"""Slash-command authorization verdict for the Discord adapter: the on_message gates, side-effect free."""
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

try:
    import discord
except ImportError:  # the adapter does not start without discord.py
    discord = None


class DiscordSlashAuthMixin:
    _get_parent_channel_id: Callable[[Any], Optional[str]]
    _discord_channel_keys_from_channel: Callable[..., set]
    _get_allowed_channels: Callable[[], set]
    _get_ignored_channels: Callable[[], set]
    _is_allowed_user: Callable[..., bool]

    # ── Slash command authorization ─────────────────────────────────────
    # ``_check_slash_authorization`` mirrors the on_message gates one-for-one. No allowlist =>
    # fail closed unless allow-all; DISCORD_ALLOWED_CHANNELS alone authorizes per validated channel.

    def _evaluate_slash_authorization(self, interaction: discord.Interaction, command_text: str='') -> tuple[bool, Optional[str]]:
        """Evaluate the user's channel, profile and slash-command authority.
            """
        chan_obj = getattr(interaction, 'channel', None)
        guild_id = getattr(interaction, 'guild_id', None)
        in_dm = isinstance(chan_obj, discord.DMChannel) or (not bool(guild_id) and getattr(interaction, 'guild', None) is None)
        if in_dm and (not self._discord_allow_dms()):
            return (False, 'Discord direct messages are disabled')
        channel_ids: set = set()
        channel_keys: set = set()
        if not in_dm:
            chan_id_raw = getattr(interaction, 'channel_id', None) or getattr(chan_obj, 'id', None)
            if chan_id_raw is not None:
                channel_ids.add(str(chan_id_raw))
                if isinstance(chan_obj, discord.Thread):
                    parent_id = self._get_parent_channel_id(chan_obj)
                    if parent_id:
                        channel_ids.add(str(parent_id))
            channel_keys = self._discord_channel_keys_from_channel(chan_obj, self._get_parent_channel_id(chan_obj) if isinstance(chan_obj, discord.Thread) else None)
            allowed = self._get_allowed_channels()
            if allowed:
                if '*' not in allowed:
                    if not channel_ids:
                        return (False, 'channel id missing with DISCORD_ALLOWED_CHANNELS configured')
                    if not channel_keys & allowed:
                        return (False, 'channel not in DISCORD_ALLOWED_CHANNELS')
            ignored = self._get_ignored_channels()
            if ignored and channel_ids:
                if '*' in ignored or channel_keys & ignored:
                    return (False, 'channel in DISCORD_IGNORED_CHANNELS')
        user = getattr(interaction, 'user', None)
        allowed_users = getattr(self, '_allowed_user_ids', set()) or set()
        allowed_roles = getattr(self, '_allowed_role_ids', set()) or set()
        if user is None or getattr(user, 'id', None) is None:
            if allowed_users or allowed_roles:
                return (False, 'missing interaction.user with allowlist configured')
            return (False, 'missing interaction.user')
        user_id = str(user.id)
        interaction_guild = getattr(interaction, 'guild', None)
        if not self._is_allowed_user(user_id, author=user, guild=interaction_guild, is_dm=in_dm, channel_ids=channel_keys if not in_dm else None):
            return (False, 'user not in DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES')
        if command_text:
            raw_command = str(command_text).strip().lstrip('/').split(maxsplit=1)[0]
            if raw_command:
                try:
                    from hermes_cli.commands import resolve_command
                    resolved = resolve_command(raw_command)
                    canonical = resolved.name if resolved else raw_command.lower()
                    source = self._build_slash_event(interaction, f'/{canonical}').source
                    runner = getattr(self, 'gateway_runner', None)
                    if runner is not None and hasattr(runner, '_check_slash_access'):
                        denial = runner._check_slash_access(source, canonical)
                    else:
                        from gateway.slash_access import policy_from_extra
                        extra = getattr(self.config, 'extra', None) or {}
                        policy = policy_from_extra(extra, 'dm' if in_dm else 'group')
                        denial = None
                        if policy.enabled and (not policy.can_run(user_id, canonical)):
                            denial = f'/{canonical} is not allowed for this user'
                    if denial:
                        return (False, f'profile slash policy denied: {denial}')
                except Exception as exc:
                    logger.warning('[Discord] Could not evaluate source-aware slash policy for %r: %s', command_text, exc, exc_info=True)
                    return (False, 'source-aware slash policy unavailable')
        return (True, None)

    def _slash_role_grant(self, interaction: discord.Interaction) -> bool:
        """``role_authorized`` for an event built from ``interaction``, with the message path's
        meaning (_discord_message_admission): a role allowlist exists and THIS actor passes the gate.
        Re-evaluated for the interaction instead of trusted from call order, so a builder can never
        stamp the grant on an actor the slash gate would refuse."""
        return bool(getattr(self, "_allowed_role_ids", set())) and self._evaluate_slash_authorization(
            interaction)[0]
