"""Durable state and broker client for friend code approvals.

This module deliberately has no model-facing entry point.  The Discord
adapter polls one fixed broker, renders owner controls, and sends exact
decisions back over a dedicated authenticated endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

from utils import atomic_json_write


_SNOWFLAKE = re.compile(r"^[0-9]{16,22}$")
_REQUEST_ID = re.compile(r"^friend_[0-9a-f]{24}$")
_REQUEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_NEW_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_TERMINAL_STATES = frozenset({
    "approved_blocked",
    "denied",
    "cancelled",
    "expired",
    "superseded",
})
_PENDING_STATE = "awaiting_owner_approval"
_MAX_RESPONSE_BYTES = 128 * 1024
_STATE_VERSION = 1


class FriendCodeRelayError(RuntimeError):
    """A validated relay or broker-contract failure."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _require_string(
    value: Any,
    label: str,
    *,
    maximum: int,
    pattern: Optional[re.Pattern[str]] = None,
) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise FriendCodeRelayError(f"invalid {label}")
    if pattern is not None and not pattern.fullmatch(text):
        raise FriendCodeRelayError(f"invalid {label}")
    return text


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise FriendCodeRelayError(f"invalid {label}")
    if not isinstance(value, (int, str)):
        raise FriendCodeRelayError(f"invalid {label}")
    if isinstance(value, str) and not value.isdigit():
        raise FriendCodeRelayError(f"invalid {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FriendCodeRelayError(f"invalid {label}") from exc
    if result < 1 or result > (2**63 - 1):
        raise FriendCodeRelayError(f"invalid {label}")
    return result


def _parse_timestamp(value: Any, label: str) -> str:
    text = _require_string(value, label, maximum=64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise FriendCodeRelayError(f"invalid {label}") from exc
    if parsed.tzinfo is None:
        raise FriendCodeRelayError(f"invalid {label}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class FriendCodeRelayConfig:
    broker_url: str
    owner_guild_id: str
    owner_channel_id: str
    owner_user_id: str
    friend_guild_id: str
    friend_channel_id: str
    poll_interval_seconds: float = 5.0
    request_timeout_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FriendCodeRelayConfig":
        raw_url = _require_string(value.get("broker_url"), "broker URL", maximum=1024)
        parsed = urlparse(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise FriendCodeRelayError("invalid broker URL")
        try:
            poll = float(value.get("poll_interval_seconds", 5.0))
            timeout = float(value.get("request_timeout_seconds", 10.0))
        except (TypeError, ValueError) as exc:
            raise FriendCodeRelayError("invalid broker timing") from exc
        if not 2.0 <= poll <= 60.0 or not 1.0 <= timeout <= 30.0:
            raise FriendCodeRelayError("invalid broker timing")
        owner_guild = _require_string(
            value.get("owner_guild_id"), "owner guild", maximum=22, pattern=_SNOWFLAKE
        )
        friend_guild = _require_string(
            value.get("friend_guild_id"), "friend guild", maximum=22, pattern=_SNOWFLAKE
        )
        if owner_guild == friend_guild:
            raise FriendCodeRelayError("owner and friend guilds must differ")
        return cls(
            broker_url=raw_url.rstrip("/"),
            owner_guild_id=owner_guild,
            owner_channel_id=_require_string(
                value.get("owner_channel_id"),
                "owner channel",
                maximum=22,
                pattern=_SNOWFLAKE,
            ),
            owner_user_id=_require_string(
                value.get("owner_user_id"), "owner user", maximum=22, pattern=_SNOWFLAKE
            ),
            friend_guild_id=friend_guild,
            friend_channel_id=_require_string(
                value.get("friend_channel_id"),
                "friend channel",
                maximum=22,
                pattern=_SNOWFLAKE,
            ),
            poll_interval_seconds=poll,
            request_timeout_seconds=timeout,
        )


@dataclass
class FriendCodeBinding:
    event_id: str
    request_id: str
    version: int
    request_hash: str
    kind: str
    request: str
    repository: Optional[dict]
    new_repository_name: str
    expires_at: str
    friend_guild_id: str
    friend_channel_id: str
    friend_thread_id: str
    friend_user_id: str
    owner_guild_id: str
    owner_channel_id: str
    owner_user_id: str
    message_id: str = ""
    state: str = _PENDING_STATE
    decision: str = ""
    friend_notified_states: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def marker(self) -> str:
        return f"Friend approval event `{self.event_id}`"

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @classmethod
    def from_notification(
        cls,
        notification: Mapping[str, Any],
        config: FriendCodeRelayConfig,
    ) -> "FriendCodeBinding":
        event_id = str(_require_positive_int(notification.get("id"), "event id"))
        if notification.get("kind") != "approval_requested":
            raise FriendCodeRelayError("unsupported notification kind")
        raw = notification.get("request")
        if not isinstance(raw, Mapping):
            raise FriendCodeRelayError("invalid request payload")
        origin = raw.get("origin")
        if not isinstance(origin, Mapping):
            raise FriendCodeRelayError("invalid request origin")
        guild = _require_string(
            origin.get("guild_id"), "friend guild", maximum=22, pattern=_SNOWFLAKE
        )
        channel = _require_string(
            origin.get("channel_id"), "friend channel", maximum=22, pattern=_SNOWFLAKE
        )
        thread = _require_string(
            origin.get("thread_id"), "friend thread", maximum=22, pattern=_SNOWFLAKE
        )
        user = _require_string(
            origin.get("user_id"), "friend user", maximum=22, pattern=_SNOWFLAKE
        )
        if (
            origin.get("platform") != "discord"
            or origin.get("profile") != "friends"
            or guild != config.friend_guild_id
            or channel != config.friend_channel_id
            or thread == channel
            or user == config.owner_user_id
        ):
            raise FriendCodeRelayError("request origin is outside the friend boundary")
        session_hash = _require_string(
            origin.get("session_hash"),
            "session hash",
            maximum=64,
            pattern=_REQUEST_HASH,
        )
        del session_hash

        kind = _require_string(raw.get("kind"), "request kind", maximum=32)
        repository = raw.get("repository")
        new_name = str(raw.get("new_repository_name") or "").strip()
        normalized_repo: Optional[dict] = None
        if kind == "existing_public":
            if not isinstance(repository, Mapping) or new_name:
                raise FriendCodeRelayError("invalid existing public repository request")
            full_name = _require_string(
                repository.get("full_name"),
                "repository",
                maximum=201,
                pattern=_REPOSITORY,
            )
            url = _require_string(repository.get("url"), "repository URL", maximum=512)
            parsed_url = urlparse(url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.netloc.lower() != "github.com"
                or parsed_url.path.strip("/").lower() != full_name.lower()
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise FriendCodeRelayError("invalid repository URL")
            if bool(repository.get("archived")) or bool(repository.get("disabled")):
                raise FriendCodeRelayError("repository is not eligible")
            normalized_repo = {
                "id": _require_positive_int(repository.get("id"), "repository id"),
                "full_name": full_name,
                "url": url,
                "default_branch": _require_string(
                    repository.get("default_branch"), "default branch", maximum=255
                ),
                "fork": bool(repository.get("fork")),
            }
        elif kind == "new_public":
            if repository is not None:
                raise FriendCodeRelayError(
                    "repository is forbidden for a new public request"
                )
            new_name = _require_string(
                new_name, "new repository name", maximum=100, pattern=_NEW_REPOSITORY
            )
        else:
            raise FriendCodeRelayError("unsupported request kind")

        state = _require_string(raw.get("state"), "request state", maximum=64)
        if state != _PENDING_STATE or raw.get("execution_ready") is not False:
            raise FriendCodeRelayError("request is not awaiting owner approval")
        return cls(
            event_id=event_id,
            request_id=_require_string(
                raw.get("id"), "request id", maximum=31, pattern=_REQUEST_ID
            ),
            version=_require_positive_int(raw.get("version"), "request version"),
            request_hash=_require_string(
                raw.get("request_hash"),
                "request hash",
                maximum=64,
                pattern=_REQUEST_HASH,
            ),
            kind=kind,
            request=_require_string(raw.get("request"), "request", maximum=4000),
            repository=normalized_repo,
            new_repository_name=new_name,
            expires_at=_parse_timestamp(raw.get("expires_at"), "expiry"),
            friend_guild_id=guild,
            friend_channel_id=channel,
            friend_thread_id=thread,
            friend_user_id=user,
            owner_guild_id=config.owner_guild_id,
            owner_channel_id=config.owner_channel_id,
            owner_user_id=config.owner_user_id,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    @classmethod
    def from_state(
        cls,
        value: Mapping[str, Any],
        config: FriendCodeRelayConfig,
    ) -> "FriendCodeBinding":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        data = {key: value.get(key) for key in allowed}
        try:
            binding = cls(**data)
        except (TypeError, ValueError) as exc:
            raise FriendCodeRelayError("invalid stored approval") from exc
        _require_string(
            binding.event_id, "event id", maximum=20, pattern=re.compile(r"^[0-9]+$")
        )
        _require_string(
            binding.request_id, "request id", maximum=31, pattern=_REQUEST_ID
        )
        binding.version = _require_positive_int(binding.version, "request version")
        binding.request_hash = _require_string(
            binding.request_hash, "request hash", maximum=64, pattern=_REQUEST_HASH
        )
        binding.request = _require_string(binding.request, "request", maximum=4000)
        binding.expires_at = _parse_timestamp(binding.expires_at, "expiry")
        binding.updated_at = _parse_timestamp(binding.updated_at, "state update")
        for attr, expected in (
            ("owner_guild_id", config.owner_guild_id),
            ("owner_channel_id", config.owner_channel_id),
            ("owner_user_id", config.owner_user_id),
            ("friend_guild_id", config.friend_guild_id),
            ("friend_channel_id", config.friend_channel_id),
        ):
            if str(getattr(binding, attr) or "") != expected:
                raise FriendCodeRelayError(
                    "stored approval crossed a configured boundary"
                )
        _require_string(
            binding.friend_thread_id, "friend thread", maximum=22, pattern=_SNOWFLAKE
        )
        _require_string(
            binding.friend_user_id, "friend user", maximum=22, pattern=_SNOWFLAKE
        )
        if (
            binding.friend_thread_id == binding.friend_channel_id
            or binding.friend_user_id == binding.owner_user_id
        ):
            raise FriendCodeRelayError("stored approval crossed a configured boundary")
        if binding.message_id:
            _require_string(
                binding.message_id, "owner message", maximum=22, pattern=_SNOWFLAKE
            )
        if binding.state not in {_PENDING_STATE, *_TERMINAL_STATES}:
            raise FriendCodeRelayError("invalid stored approval state")
        if binding.decision not in {"", "approve", "deny"}:
            raise FriendCodeRelayError("invalid stored approval decision")
        if (
            (binding.state == _PENDING_STATE and binding.decision)
            or (binding.state == "approved_blocked" and binding.decision != "approve")
            or (binding.state == "denied" and binding.decision != "deny")
            or (
                binding.state in {"cancelled", "expired", "superseded"}
                and binding.decision
            )
        ):
            raise FriendCodeRelayError("stored approval decision is inconsistent")
        if not isinstance(binding.friend_notified_states, list):
            raise FriendCodeRelayError("invalid stored friend delivery state")
        binding.friend_notified_states = [
            state
            for state in binding.friend_notified_states
            if state in _TERMINAL_STATES
        ]
        if binding.kind == "existing_public":
            if (
                not isinstance(binding.repository, Mapping)
                or binding.new_repository_name
            ):
                raise FriendCodeRelayError("invalid stored repository request")
            _require_positive_int(binding.repository.get("id"), "repository id")
            full_name = _require_string(
                binding.repository.get("full_name"),
                "repository",
                maximum=201,
                pattern=_REPOSITORY,
            )
            url = _require_string(
                binding.repository.get("url"), "repository URL", maximum=512
            )
            parsed_url = urlparse(url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.netloc.lower() != "github.com"
                or parsed_url.path.strip("/").lower() != full_name.lower()
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise FriendCodeRelayError("invalid stored repository URL")
            _require_string(
                binding.repository.get("default_branch"), "default branch", maximum=255
            )
        elif binding.kind == "new_public":
            if binding.repository is not None:
                raise FriendCodeRelayError("invalid stored repository request")
            binding.new_repository_name = _require_string(
                binding.new_repository_name,
                "new repository name",
                maximum=100,
                pattern=_NEW_REPOSITORY,
            )
        else:
            raise FriendCodeRelayError("invalid stored request kind")
        return binding

    def button_custom_id(self, decision: str, owner_token: str) -> str:
        if decision not in {"approve", "deny"} or not self.message_id:
            raise FriendCodeRelayError("approval button is not fully bound")
        payload = "\x00".join((
            "hfc1",
            decision,
            self.event_id,
            self.request_id,
            str(self.version),
            self.request_hash,
            self.message_id,
            self.owner_guild_id,
            self.owner_channel_id,
            self.owner_user_id,
        )).encode("utf-8")
        digest = hmac.new(
            owner_token.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()[:40]
        short = "a" if decision == "approve" else "d"
        return f"hfc1:{short}:{digest}"

    def decision_idempotency_key(self, decision: str, owner_token: str) -> str:
        custom_id = self.button_custom_id(decision, owner_token)
        return hashlib.sha256(custom_id.encode("ascii")).hexdigest()

    def verify_custom_id(self, custom_id: str, decision: str, owner_token: str) -> bool:
        try:
            expected = self.button_custom_id(decision, owner_token)
        except FriendCodeRelayError:
            return False
        return hmac.compare_digest(str(custom_id or ""), expected)

    def to_dict(self) -> dict:
        return asdict(self)


class FriendCodeStateStore:
    """Atomic owner-only JSON state for restart-safe Discord views."""

    def __init__(self, path: Path, config: FriendCodeRelayConfig):
        self.path = Path(path)
        self.config = config

    def load(self) -> dict[str, FriendCodeBinding]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FriendCodeRelayError("approval state could not be read") from exc
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise FriendCodeRelayError("unsupported approval state")
        raw_bindings = payload.get("bindings")
        if not isinstance(raw_bindings, dict) or len(raw_bindings) > 500:
            raise FriendCodeRelayError("invalid approval state")
        bindings: dict[str, FriendCodeBinding] = {}
        for event_id, raw in raw_bindings.items():
            if not isinstance(raw, Mapping):
                raise FriendCodeRelayError("invalid stored approval")
            binding = FriendCodeBinding.from_state(raw, self.config)
            if event_id != binding.event_id or event_id in bindings:
                raise FriendCodeRelayError("stored approval event mismatch")
            bindings[event_id] = binding
        return bindings

    def save(self, bindings: Mapping[str, FriendCodeBinding]) -> None:
        ordered = sorted(
            bindings.items(),
            key=lambda item: (not item[1].is_terminal, int(item[0])),
        )
        if len(ordered) > 500:
            ordered = ordered[-500:]
        atomic_json_write(
            self.path,
            {
                "version": _STATE_VERSION,
                "bindings": {key: binding.to_dict() for key, binding in ordered},
            },
            mode=0o600,
            indent=None,
            separators=(",", ":"),
        )


class FriendCodeBrokerClient:
    """Bounded HTTP client for the broker's non-model relay endpoints."""

    def __init__(
        self,
        config: FriendCodeRelayConfig,
        *,
        delivery_token: str,
        owner_token: str,
    ):
        self.config = config
        self.delivery_token = _require_string(
            delivery_token, "delivery token", maximum=4096
        )
        self.owner_token = _require_string(owner_token, "owner token", maximum=4096)
        if hmac.compare_digest(self.delivery_token, self.owner_token):
            raise FriendCodeRelayError("delivery and owner credentials must differ")

    async def notifications(self) -> dict:
        return await self._request(
            "GET",
            "/v1/discord/friend-code/notifications",
            token=self.delivery_token,
        )

    async def acknowledge(self, binding: FriendCodeBinding) -> dict:
        return await self._request(
            "POST",
            "/v1/discord/friend-code/notifications/"
            + quote(binding.event_id, safe="")
            + "/ack",
            token=self.delivery_token,
            body={
                "request": binding.request_id,
                "version": binding.version,
                "hash": binding.request_hash,
                "message_id": binding.message_id,
            },
        )

    async def decide(
        self,
        binding: FriendCodeBinding,
        decision: str,
        *,
        clicker_guild_id: str,
        clicker_channel_id: str,
        clicker_user_id: str,
    ) -> dict:
        if decision not in {"approve", "deny"}:
            raise FriendCodeRelayError("invalid owner decision")
        return await self._request(
            "POST",
            "/v1/discord/friend-code/decisions",
            token=self.owner_token,
            idempotency_key=binding.decision_idempotency_key(
                decision, self.owner_token
            ),
            body={
                "event": _require_positive_int(binding.event_id, "event id"),
                "request": binding.request_id,
                "version": binding.version,
                "hash": binding.request_hash,
                "decision": decision,
                "message_id": binding.message_id,
                "clicker": {
                    "platform": "discord",
                    "profile": "default",
                    "guild_id": clicker_guild_id,
                    "channel_id": clicker_channel_id,
                    "user_id": clicker_user_id,
                },
            },
        )

    async def decision_receipt(self, binding: FriendCodeBinding, decision: str) -> dict:
        return await self._request(
            "GET",
            "/v1/discord/friend-code/decisions/" + quote(binding.event_id, safe=""),
            token=self.owner_token,
            idempotency_key=binding.decision_idempotency_key(
                decision, self.owner_token
            ),
        )

    async def acknowledge_receipt(self, binding: FriendCodeBinding, state: str) -> dict:
        return await self._request(
            "POST",
            "/v1/discord/friend-code/receipts/"
            + quote(binding.event_id, safe="")
            + "/ack",
            token=self.delivery_token,
            body={
                "request": binding.request_id,
                "version": binding.version,
                "hash": binding.request_hash,
                "state": state,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: Optional[dict] = None,
        idempotency_key: str = "",
    ) -> dict:
        import aiohttp

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            try:
                async with session.request(
                    method,
                    self.config.broker_url + path,
                    headers=headers,
                    json=body,
                    allow_redirects=False,
                ) as response:
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(16 * 1024):
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            raise FriendCodeRelayError(
                                "broker response exceeded its size limit"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    try:
                        payload = json.loads(raw.decode("utf-8")) if raw else {}
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise FriendCodeRelayError(
                            "broker returned an invalid response",
                            status=response.status,
                        ) from exc
                    if response.status < 200 or response.status >= 300:
                        code = (
                            payload.get("code") if isinstance(payload, dict) else None
                        )
                        message = str(code or "broker request failed")
                        raise FriendCodeRelayError(
                            message,
                            status=response.status,
                            retryable=response.status >= 500
                            or response.status in {408, 429},
                        )
                    if not isinstance(payload, dict):
                        raise FriendCodeRelayError(
                            "broker returned an invalid response"
                        )
                    return payload
            except FriendCodeRelayError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise FriendCodeRelayError(
                    "broker is unavailable", retryable=True
                ) from exc


def validate_delivery_payload(
    payload: Mapping[str, Any], config: FriendCodeRelayConfig
) -> tuple[list[dict], list[dict]]:
    """Validate the fixed owner destination and bounded event lists."""
    if (
        str(payload.get("guild_id") or "") != config.owner_guild_id
        or str(payload.get("channel_id") or "") != config.owner_channel_id
    ):
        raise FriendCodeRelayError("broker returned the wrong owner destination")
    notifications = payload.get("notifications")
    if not isinstance(notifications, list) or len(notifications) > 25:
        raise FriendCodeRelayError("broker returned an invalid notification list")
    if not all(isinstance(item, dict) for item in notifications):
        raise FriendCodeRelayError("broker returned an invalid notification")
    receipts = payload.get("receipts", [])
    if not isinstance(receipts, list) or len(receipts) > 25:
        raise FriendCodeRelayError("broker returned an invalid receipt list")
    if not all(isinstance(item, dict) for item in receipts):
        raise FriendCodeRelayError("broker returned an invalid receipt")
    return notifications, receipts


def validate_decision_receipt(
    payload: Mapping[str, Any], binding: FriendCodeBinding, decision: str
) -> str:
    """Return a terminal state only when the receipt matches the clicked card."""
    expected_state = "approved_blocked" if decision == "approve" else "denied"
    if (
        str(payload.get("request") or "") != binding.request_id
        or _require_positive_int(payload.get("version"), "receipt version")
        != binding.version
        or str(payload.get("hash") or "") != binding.request_hash
        or str(payload.get("decision") or "") != decision
        or str(payload.get("state") or "") != expected_state
        or payload.get("execution_ready") is not False
    ):
        raise FriendCodeRelayError(
            "broker decision receipt did not match the clicked card"
        )
    return expected_state


def validate_status_receipt(
    payload: Mapping[str, Any],
    binding: FriendCodeBinding,
    config: FriendCodeRelayConfig,
) -> str:
    """Validate a broker-emitted status without accepting owner metadata."""
    state = _require_string(payload.get("state"), "receipt state", maximum=64)
    if state not in _TERMINAL_STATES:
        raise FriendCodeRelayError("receipt is not terminal")
    if (
        str(payload.get("event") or "") != binding.event_id
        or str(payload.get("request") or "") != binding.request_id
        or _require_positive_int(payload.get("version"), "receipt version")
        != binding.version
        or str(payload.get("hash") or "") != binding.request_hash
        or payload.get("execution_ready") is not False
    ):
        raise FriendCodeRelayError(
            "broker status receipt did not match its approval card"
        )
    origin = payload.get("friend_origin")
    if not isinstance(origin, Mapping) or (
        str(origin.get("guild_id") or "") != config.friend_guild_id
        or str(origin.get("channel_id") or "") != config.friend_channel_id
        or str(origin.get("thread_id") or "") != binding.friend_thread_id
        or str(origin.get("user_id") or "") != binding.friend_user_id
    ):
        raise FriendCodeRelayError("broker status receipt crossed the friend boundary")
    return state


def friend_status_content(binding: FriendCodeBinding, state: str) -> str:
    """Format a fixed, owner-metadata-free friend status message."""
    messages = {
        "approved_blocked": "was approved. Execution is not available yet.",
        "denied": "was declined.",
        "cancelled": "was cancelled.",
        "expired": "expired before approval.",
        "superseded": "was replaced by a newer request.",
    }
    if state not in messages:
        raise FriendCodeRelayError("unsupported friend status")
    return f"Code request `{binding.request_id}` {messages[state]}"
