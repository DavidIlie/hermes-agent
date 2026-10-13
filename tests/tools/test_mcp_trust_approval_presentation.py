from unittest.mock import patch

from tools import approval, approval_context, approval_prompt, approval_gateway_wait


def test_mcp_trust_approval_is_single_use_and_carries_action_label():
    captured = {}
    session_key = "discord:test"

    def notify(_data):
        raise AssertionError("the wait helper should capture before notifying")

    def decide(_session_key, _notify, approval_data, *, surface):
        captured.update(approval_data)
        captured["surface"] = surface
        return {"resolved": True, "choice": "once"}

    token = approval_context.set_current_session_key(session_key)
    approval.register_gateway_notify(session_key, notify)
    try:
        with (
            patch.object(approval_context, "_is_gateway_approval_context", return_value=True),
            patch.object(approval_gateway_wait, "_await_gateway_decision", side_effect=decide),
        ):
            result = approval_prompt.request_elicitation_consent(
                "Run task_merge",
                "Merge the recorded pull request",
                surface="mcp-trust/fleet_delivery_actions",
                action_label="Merge PR",
            )
    finally:
        approval.unregister_gateway_notify(session_key)
        approval_context.reset_current_session_key(token)

    assert result == "accept"
    assert captured["action_label"] == "Merge PR"
    assert captured["allow_session"] is False
    assert captured["allow_permanent"] is False
    assert captured["require_explicit_user"] is True
    assert captured["surface"] == "mcp-trust/fleet_delivery_actions"
