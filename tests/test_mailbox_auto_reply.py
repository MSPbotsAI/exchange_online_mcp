"""exo_get_auto_reply / exo_set_auto_reply: read/write, partial-update
preservation, and offset-aware UTC conversion (PRD-20394)."""

import pytest
from conftest import tool_json

from exo_mcp.config import Settings
from exo_mcp.server import _credentials_var, create_mcp_server

AUTO_REPLY_RECORD = {
    "AutoReplyState": "Scheduled",
    "ExternalAudience": "Known",
    "InternalMessage": "In the office next week.",
    "ExternalMessage": "I am out of office.",
    "StartTime": "2026-01-09T23:00:00Z",
    "EndTime": "2026-01-16T23:00:00Z",
}


@pytest.fixture
def mcp():
    return create_mcp_server(Settings(entra_login_base_url="https://login.example.test"))


async def call(mcp, credentials, name, args):
    token = _credentials_var.set(credentials)
    try:
        return tool_json(await mcp.call_tool(name, args))
    finally:
        _credentials_var.reset(token)


@pytest.mark.asyncio
async def test_get_auto_reply_returns_current_config(mcp, upstream, credentials):
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = [AUTO_REPLY_RECORD]

    result = await call(mcp, credentials, "exo_get_auto_reply", {"identity": "alice@contoso.com"})

    assert result == {
        "identity": "alice@contoso.com",
        "auto_reply_state": "Scheduled",
        "external_audience": "Known",
        "internal_message": "In the office next week.",
        "external_message": "I am out of office.",
        "start_time": "2026-01-09T23:00:00Z",
        "end_time": "2026-01-16T23:00:00Z",
    }
    assert not [
        r for r in upstream.invoke_requests if r["cmdlet"] != "Get-MailboxAutoReplyConfiguration"
    ]


@pytest.mark.asyncio
async def test_get_auto_reply_not_found(mcp, upstream, credentials):
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = []

    result = await call(mcp, credentials, "exo_get_auto_reply", {"identity": "ghost@contoso.com"})

    assert result["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_set_auto_reply_preserves_unsupplied_fields(mcp, upstream, credentials):
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = [AUTO_REPLY_RECORD]

    await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "internal_message": "Back Monday."},
    )

    set_calls = [
        r for r in upstream.invoke_requests if r["cmdlet"] == "Set-MailboxAutoReplyConfiguration"
    ]
    assert len(set_calls) == 1
    assert set_calls[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "InternalMessage": "Back Monday.",
    }


@pytest.mark.asyncio
async def test_set_auto_reply_preserves_unsupplied_fields_in_readback(mcp, upstream, credentials):
    """The post-write readback still shows the original audience/message the
    caller did not touch — the mock upstream is stateless, so this checks the
    tool reads back rather than echoing the request, by returning a fixed
    canned record regardless of what was just set."""
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = [AUTO_REPLY_RECORD]

    result = await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "internal_message": "Back Monday."},
    )

    assert result["external_audience"] == "Known"
    assert result["external_message"] == "I am out of office."


@pytest.mark.asyncio
async def test_set_auto_reply_converts_brisbane_local_to_utc(mcp, upstream, credentials):
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = [AUTO_REPLY_RECORD]

    await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "start_time": "2026-01-10T09:00:00+10:00"},
    )

    set_calls = [
        r for r in upstream.invoke_requests if r["cmdlet"] == "Set-MailboxAutoReplyConfiguration"
    ]
    assert set_calls[0]["parameters"]["StartTime"] == "2026-01-09T23:00:00Z"


@pytest.mark.asyncio
async def test_set_auto_reply_rejects_naive_datetime(mcp, upstream, credentials):
    result = await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "start_time": "2026-01-10T09:00:00"},
    )

    assert result["error"]["code"] == "invalid_argument"
    assert not upstream.invoke_requests


@pytest.mark.asyncio
async def test_set_auto_reply_reads_back_exactly_once(mcp, upstream, credentials):
    upstream.responses["Get-MailboxAutoReplyConfiguration"] = [AUTO_REPLY_RECORD]

    await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "auto_reply_state": "Enabled"},
    )

    read_calls = [
        r for r in upstream.invoke_requests if r["cmdlet"] == "Get-MailboxAutoReplyConfiguration"
    ]
    assert len(read_calls) == 1


@pytest.mark.asyncio
async def test_set_auto_reply_readback_failure_reports_unverified(mcp, upstream, credentials):
    upstream.cmdlet_errors["Get-MailboxAutoReplyConfiguration"] = (
        500,
        {"error": {"message": "temporary upstream failure"}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "auto_reply_state": "Enabled"},
    )

    assert result == {
        "identity": "alice@contoso.com",
        "write_succeeded": True,
        "verified": False,
    }


@pytest.mark.asyncio
async def test_set_auto_reply_not_found(mcp, upstream, credentials):
    upstream.cmdlet_errors["Set-MailboxAutoReplyConfiguration"] = (
        400,
        {"error": {"message": "Couldn't find object \"ghost@contoso.com\"."}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "ghost@contoso.com", "auto_reply_state": "Enabled"},
    )

    assert result["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_set_auto_reply_rejects_invalid_enum_value(mcp, upstream, credentials):
    result = await call(
        mcp,
        credentials,
        "exo_set_auto_reply",
        {"identity": "alice@contoso.com", "auto_reply_state": "Maybe"},
    )

    assert result["error"]["code"] == "invalid_argument"
    assert "Disabled" in result["error"]["message"]
    assert not upstream.invoke_requests
