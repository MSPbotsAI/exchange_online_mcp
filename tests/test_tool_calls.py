"""Tool-layer behaviour: field projection, the shared-mailbox gate, truncation
and the exact cmdlet parameters each write tool sends."""

import pytest
from conftest import tool_json

from exo_mcp.config import Settings
from exo_mcp.server import _credentials_var, create_mcp_server

# A Get-Mailbox record as the admin endpoint really returns it: whitelisted
# properties, @odata companions, and a long tail the agent must never see.
MAILBOX_RECORD = {
    "Identity": "contoso.onmicrosoft.com/Users/alice",
    "Identity@odata.type": "#String",
    "UserPrincipalName": "alice@contoso.com",
    "PrimarySmtpAddress": "alice@contoso.com",
    "DisplayName": "Alice Smith",
    "RecipientTypeDetails": "UserMailbox",
    "HiddenFromAddressListsEnabled": False,
    "LitigationHoldEnabled": False,
    "InPlaceHolds": [],
    "ArchiveStatus": "None",
    "AccountDisabled": True,
    "ProhibitSendReceiveQuota": "50 GB (53,687,091,200 bytes)",
    "WhenMailboxCreated": "2019-04-02T13:22:11",
    "ExtensionCustomAttribute1": ["noise"],
    "ThrottlingPolicy": "",
    "MessageTrackingReadStatusEnabled": True,
}

STATS_SMALL = [{"TotalItemSize": "1.66 GB (1,782,579,200 bytes)", "ItemCount": 12043,
                "LastLogonTime": "2026-08-20T08:15:00"}]


@pytest.fixture
def mcp():
    return create_mcp_server(
        Settings(entra_login_base_url="https://login.example.test")
    )


async def call(mcp, credentials, name, args):
    token = _credentials_var.set(credentials)
    try:
        return tool_json(await mcp.call_tool(name, args))
    finally:
        _credentials_var.reset(token)


@pytest.mark.asyncio
async def test_get_mailbox_projects_fields_and_clears_the_shared_gate(
    mcp, upstream, credentials
):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    upstream.responses["Get-MailboxStatistics"] = STATS_SMALL

    result = await call(mcp, credentials, "exo_get_mailbox", {"identity": "alice@contoso.com"})

    assert result["mailbox"]["user_principal_name"] == "alice@contoso.com"
    assert result["mailbox"]["recipient_type_details"] == "UserMailbox"
    assert result["mailbox"]["account_disabled"] is True
    # Noise and @odata companions are gone.
    assert "ThrottlingPolicy" not in str(result)
    assert "@odata" not in str(result)
    assert result["statistics"]["item_count"] == 12043
    gate = result["shared_conversion"]
    assert gate["size_bytes"] == 1782579200
    assert gate["size_gb"] == 1.66
    assert gate["over_limit"] is False
    assert gate["license_required"] is False


@pytest.mark.asyncio
async def test_get_mailbox_flags_when_a_licence_must_stay(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = [{**MAILBOX_RECORD, "ArchiveStatus": "Active"}]
    upstream.responses["Get-MailboxStatistics"] = [
        {"TotalItemSize": "62.4 GB (67,000,000,000 bytes)", "ItemCount": 900000}
    ]

    gate = (
        await call(mcp, credentials, "exo_get_mailbox", {"identity": "alice@contoso.com"})
    )["shared_conversion"]

    assert gate["over_limit"] is True
    assert gate["archive_enabled"] is True
    assert gate["license_required"] is True


@pytest.mark.asyncio
async def test_get_mailbox_survives_missing_statistics(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    upstream.cmdlet_errors["Get-MailboxStatistics"] = (
        400,
        {"error": {"message": "The user hasn't logged on to mailbox 'alice'."}},
    )

    result = await call(mcp, credentials, "exo_get_mailbox", {"identity": "alice@contoso.com"})

    assert result["mailbox"]["display_name"] == "Alice Smith"
    assert result["statistics"] is None
    assert "hasn't logged on" in result["statistics_error"]
    assert result["shared_conversion"]["size_unknown"] is True


@pytest.mark.asyncio
async def test_get_mailbox_reports_not_found(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = []
    result = await call(mcp, credentials, "exo_get_mailbox", {"identity": "ghost@contoso.com"})
    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_get_mailbox_can_skip_statistics(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    result = await call(
        mcp,
        credentials,
        "exo_get_mailbox",
        {"identity": "alice@contoso.com", "include_statistics": False},
    )
    assert "statistics" not in result
    assert [r["cmdlet"] for r in upstream.invoke_requests] == ["Get-Mailbox"]


@pytest.mark.asyncio
async def test_convert_mailbox_to_shared_sets_type_and_verifies(mcp, upstream, credentials):
    upstream.responses["Set-Mailbox"] = []
    upstream.responses["Get-Mailbox"] = [
        {**MAILBOX_RECORD, "RecipientTypeDetails": "SharedMailbox"}
    ]

    result = await call(
        mcp, credentials, "exo_convert_mailbox_to_shared", {"identity": "alice@contoso.com"}
    )

    assert result == {
        "identity": "alice@contoso.com",
        "converted": True,
        "recipient_type_details": "SharedMailbox",
    }
    assert upstream.invoke_requests[0]["cmdlet"] == "Set-Mailbox"
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "Type": "Shared",
    }


@pytest.mark.asyncio
async def test_set_mailbox_hidden_reports_the_read_back_value(mcp, upstream, credentials):
    upstream.responses["Set-Mailbox"] = []
    upstream.responses["Get-Mailbox"] = [
        {**MAILBOX_RECORD, "HiddenFromAddressListsEnabled": True}
    ]

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_hidden",
        {"identity": "alice@contoso.com", "hidden": True},
    )

    assert result["hidden_from_address_lists_enabled"] is True
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "HiddenFromAddressListsEnabled": True,
    }


@pytest.mark.asyncio
async def test_remove_distribution_group_member_parameters(mcp, upstream, credentials):
    upstream.responses["Remove-DistributionGroupMember"] = []

    result = await call(
        mcp,
        credentials,
        "exo_remove_distribution_group_member",
        {"group": "all-staff@contoso.com", "member": "alice@contoso.com"},
    )

    assert result == {
        "group": "all-staff@contoso.com",
        "member": "alice@contoso.com",
        "removed": True,
    }
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "all-staff@contoso.com",
        "Member": "alice@contoso.com",
        "BypassSecurityGroupManagerCheck": True,
        # Confirm:false keeps the cmdlet from waiting on an interactive prompt.
        "Confirm": False,
    }


@pytest.mark.asyncio
async def test_list_mobile_devices_projects_and_truncates(mcp, upstream, credentials):
    upstream.responses["Get-MobileDevice"] = [
        {
            "Guid": f"guid-{i}",
            "Guid@odata.type": "#Guid",
            "DeviceId": f"dev{i}",
            "DeviceType": "iPhone",
            "DeviceModel": "iPhone14",
            "DeviceOS": "iOS 18.1",
            "FriendlyName": f"Alice iPhone {i}",
            "ClientType": "EAS",
            "DeviceAccessState": "Allowed",
            "FirstSyncTime": "2025-01-05T10:00:00",
            "WhenChangedUTC": "noise",
        }
        for i in range(5)
    ]

    result = await call(
        mcp,
        credentials,
        "exo_list_mobile_devices",
        {"mailbox": "alice@contoso.com", "limit": 2},
    )

    assert result["count"] == 2
    assert result["has_more"] is True
    assert result["devices"][0]["guid"] == "guid-0"
    assert "WhenChangedUTC" not in str(result)
    assert upstream.invoke_requests[0]["headers"]["prefer"] == "odata.maxpagesize=2"


@pytest.mark.asyncio
async def test_list_mobile_devices_empty_is_not_an_error(mcp, upstream, credentials):
    upstream.responses["Get-MobileDevice"] = []
    result = await call(
        mcp, credentials, "exo_list_mobile_devices", {"mailbox": "alice@contoso.com"}
    )
    assert result == {
        "mailbox": "alice@contoso.com",
        "devices": [],
        "count": 0,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_remove_mobile_device_parameters(mcp, upstream, credentials):
    upstream.responses["Remove-MobileDevice"] = []
    result = await call(mcp, credentials, "exo_remove_mobile_device", {"device_id": "guid-1"})
    assert result == {"device_id": "guid-1", "removed": True}
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "guid-1",
        "Confirm": False,
    }
