from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, ExecApprovalView


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_prompt_uses_visible_content_with_command_and_reason():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    command = "python scripts/deploy.py --env prod --force"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=command,
        session_key="discord:555",
        description="script execution via -c flag",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None

    prompt_text = sent["content"]
    assert "Command Approval Required" in prompt_text
    assert "Do you want Hermes to run this command?" in prompt_text
    assert "Requested command" in prompt_text
    assert command in prompt_text
    assert "Reason" in prompt_text
    assert "script execution via -c flag" in prompt_text


@pytest.mark.asyncio
async def test_mcp_action_uses_one_task_specific_button():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_exec_approval(
        chat_id="555",
        command="MCP tool 'task_merge' wants to run",
        session_key="discord:555",
        description="Merge the recorded pull request",
        allow_session=False,
        allow_permanent=False,
        action_label="Merge PR",
        require_explicit_user=True,
    )

    assert result.success is True
    view = sent["view"]
    assert view.action_label == "Merge PR"
    assert sent["embed"].title == "Merge PR"
    assert "Confirm this one operation" in sent["content"]


def test_task_specific_button_rejects_another_discord_user():
    view = ExecApprovalView(
        session_key="discord:555",
        allowed_user_ids={"243009043260637184"},
        allowed_role_ids={7},
        allow_session=False,
        allow_permanent=False,
        action_label="Merge PR",
        require_explicit_user=True,
    )

    authorized = SimpleNamespace(user=SimpleNamespace(id="243009043260637184"))
    unauthorized = SimpleNamespace(
        user=SimpleNamespace(id="999", roles=[SimpleNamespace(id=7)])
    )
    assert view._check_auth(authorized) is True
    assert view._check_auth(unauthorized) is False
