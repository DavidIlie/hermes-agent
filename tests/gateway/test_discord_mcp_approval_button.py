from plugins.platforms.discord.adapter import ExecApprovalView


def test_mcp_action_has_only_merge_and_deny_buttons():
    view = ExecApprovalView(
        session_key="discord:555",
        allowed_user_ids={"243009043260637184"},
        allow_session=False,
        allow_permanent=False,
        action_label="Merge PR",
        require_explicit_user=True,
    )

    assert [child.label for child in view.children] == ["Merge PR", "Deny"]
