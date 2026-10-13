from typing import Any, Optional
from tools.registry import tool_error
from tools.mcp_tool_common import mcp_field

_TRANSPORT_PROVENANCE_ARG = "_hermes_provenance"

def _with_trusted_transport_provenance(
    server_name: str,
    tool_name: str,
    args: dict,
    *,
    enabled: bool,
    required: bool,
) -> tuple[dict, Optional[str]]:
    """Attach authenticated Discord origin to an opted-in MCP tool.

    The MCP argument object is model-controlled, including reserved-looking
    keys. Remove any supplied provenance first, then source the replacement
    exclusively from task-local gateway ContextVars. Environment fallback is
    deliberately forbidden because it cannot prove which authenticated
    session commissioned the call.

    A named multiplex profile is an explicit trust boundary. Such calls fail
    closed unless they came from a fully identified Discord thread. The
    default profile keeps the historical owner behavior: complete Discord
    context is attached when available, while CLI and other transports remain
    usable without claiming provenance.
    """
    if not enabled:
        return args, None

    prepared = dict(args or {})
    prepared.pop(_TRANSPORT_PROVENANCE_ARG, None)

    try:
        from gateway.session_context import get_bound_session_env
    except Exception as exc:
        if required:
            return prepared, tool_error(
                f"MCP tool '{tool_name}' on server '{server_name}' requires "
                "transport-authenticated provenance, but the gateway session "
                f"context is unavailable ({type(exc).__name__}). The tool was NOT called."
            )
        return prepared, None

    try:
        platform = (
            get_bound_session_env("HERMES_SESSION_PLATFORM") or ""
        ).strip().lower()
        profile = (get_bound_session_env("HERMES_SESSION_PROFILE") or "").strip()
        chat_id = (get_bound_session_env("HERMES_SESSION_CHAT_ID") or "").strip()
        thread_id = (
            get_bound_session_env("HERMES_SESSION_THREAD_ID") or ""
        ).strip()
        parent_chat_id = (
            get_bound_session_env("HERMES_SESSION_PARENT_CHAT_ID") or ""
        ).strip()
        guild_id = (
            get_bound_session_env("HERMES_SESSION_SCOPE_ID") or ""
        ).strip()
        session_key = (
            get_bound_session_env("HERMES_SESSION_KEY") or ""
        ).strip()
        user_id = (
            get_bound_session_env("HERMES_SESSION_USER_ID") or ""
        ).strip()
    except Exception as exc:
        if required:
            return prepared, tool_error(
                f"MCP tool '{tool_name}' on server '{server_name}' requires "
                "transport-authenticated provenance, but the gateway session "
                f"context could not be read ({type(exc).__name__}). The tool was NOT called."
            )
        return prepared, None
    channel_id = parent_chat_id or chat_id

    if profile or required:
        missing = []
        if required and not profile:
            missing.append("multiplex profile")
        if platform != "discord":
            missing.append("Discord transport")
        for label, value in (
            ("guild", guild_id),
            ("parent channel", parent_chat_id),
            ("thread", thread_id),
            ("requester", user_id),
            ("session", session_key),
        ):
            if not value:
                missing.append(label)
        if chat_id != thread_id:
            missing.append("thread routing match")
        if missing:
            return prepared, tool_error(
                f"MCP tool '{tool_name}' on server '{server_name}' requires "
                f"complete transport-authenticated Discord thread provenance "
                f"for multiplex profile '{profile}'. Missing or invalid: "
                f"{', '.join(missing)}. The tool was NOT called."
            )

    if platform != "discord":
        return prepared, None

    # A platform marker by itself is not useful provenance. For the default
    # profile, preserve the established owner behavior by forwarding no claim
    # unless the minimum authenticated origin is complete.
    if not channel_id or not session_key or not user_id:
        return prepared, None

    provenance = {
        "platform": "discord",
        "channel_id": channel_id,
        "session_key": session_key,
        "user_id": user_id,
    }
    if profile:
        provenance["profile"] = profile
    if guild_id:
        provenance["guild_id"] = guild_id
    if thread_id:
        provenance["thread_id"] = thread_id
    prepared[_TRANSPORT_PROVENANCE_ARG] = provenance
    return prepared, None

def _mcp_tool_accepts_transport_provenance(mcp_tool: Any) -> bool:
    """Return whether a server schema opts into trusted origin injection."""
    raw_schema = mcp_field(mcp_tool, "input_schema", "inputSchema")
    if not isinstance(raw_schema, dict):
        return False
    properties = raw_schema.get("properties")
    return (
        isinstance(properties, dict)
        and _TRANSPORT_PROVENANCE_ARG in properties
    )

def _mcp_tool_requires_transport_provenance(mcp_tool: Any) -> bool:
    """Return whether the server requires the reserved transport envelope."""
    raw_schema = mcp_field(mcp_tool, "input_schema", "inputSchema")
    if not isinstance(raw_schema, dict):
        return False
    required = raw_schema.get("required")
    return (
        isinstance(required, list)
        and _TRANSPORT_PROVENANCE_ARG in required
    )
