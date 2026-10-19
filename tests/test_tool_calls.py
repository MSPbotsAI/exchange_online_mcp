"""Tool-layer behaviour: field projection, the shared-mailbox gate, truncation
and the exact cmdlet parameters each write tool sends."""

import asyncio

import pytest
from conftest import CLIENT_ID, TENANT, tool_json

from exo_mcp.auth import ExoCredentials
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
async def test_set_mailbox_hidden_reports_pending_not_verified(mcp, upstream, credentials):
    """PRD-20321: a same-call read hits the server that took the write and
    echoes the new value, while other servers lag by minutes. The tool must
    not present any readback as confirmation."""
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

    assert result["requested_hidden"] is True
    assert result["write_accepted"] is True
    assert result["verified"] is False
    assert "exo_get_mailbox" in result["note"]
    assert "hidden_from_address_lists_enabled" not in result
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "HiddenFromAddressListsEnabled": True,
    }
    assert all(r["cmdlet"] != "Get-Mailbox" for r in upstream.invoke_requests)


@pytest.mark.asyncio
async def test_set_mailbox_hidden_unhide_is_also_unverified(mcp, upstream, credentials):
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_hidden",
        {"identity": "alice@contoso.com", "hidden": False},
    )

    assert result["requested_hidden"] is False
    assert result["verified"] is False
    assert upstream.invoke_requests[0]["parameters"]["HiddenFromAddressListsEnabled"] is False


@pytest.mark.asyncio
async def test_set_mailbox_hidden_reports_not_found(mcp, upstream, credentials):
    """PRD-20321 / TC-EXO-004 (regression guard): an identity that resolves to no
    mailbox must still fail at the write step with the existing 404-equivalent
    ExoError behaviour."""
    upstream.cmdlet_errors["Set-Mailbox"] = (
        404,
        {"error": {"message": "Couldn't find object \"ghost@contoso.com\"."}},
    )

    result = await call(
        mcp, credentials, "exo_set_mailbox_hidden", {"identity": "ghost@contoso.com", "hidden": True}
    )

    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False
    assert all(r["cmdlet"] != "Get-Mailbox" for r in upstream.invoke_requests)


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
        # No BypassSecurityGroupManagerCheck by default: sending it at all needs
        # Organization Management (PRD-20394 INT finding).
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


FORWARDED_RECORD = {
    **MAILBOX_RECORD,
    "ForwardingAddress": "contoso.onmicrosoft.com/Users/bob",
    "DeliverToMailboxAndForward": True,
}


@pytest.mark.asyncio
async def test_set_forwarding_targets_the_internal_field_and_keeps_a_copy(
    mcp, upstream, credentials
):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_forwarding",
        {"identity": "alice@contoso.com", "forward_to": "bob@contoso.com"},
    )

    set_call = [r for r in upstream.invoke_requests if r["cmdlet"] == "Set-Mailbox"][0]
    assert set_call["parameters"] == {
        "Identity": "alice@contoso.com",
        # Internal-only by construction: ForwardingAddress refuses anything
        # that is not a recipient in the tenant.
        "ForwardingAddress": "bob@contoso.com",
        # Cleared so an external target set elsewhere cannot survive alongside.
        "ForwardingSmtpAddress": None,
        "DeliverToMailboxAndForward": True,
    }
    assert result["previous_forwarding"] == {}


@pytest.mark.asyncio
async def test_set_forwarding_can_forward_without_keeping_a_copy(
    mcp, upstream, credentials
):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    upstream.responses["Set-Mailbox"] = []

    await call(
        mcp,
        credentials,
        "exo_set_mailbox_forwarding",
        {
            "identity": "alice@contoso.com",
            "forward_to": "bob@contoso.com",
            "deliver_and_forward": False,
        },
    )

    set_call = [r for r in upstream.invoke_requests if r["cmdlet"] == "Set-Mailbox"][0]
    assert set_call["parameters"]["DeliverToMailboxAndForward"] is False


@pytest.mark.asyncio
async def test_clearing_forwarding_empties_every_field_and_reports_the_old_one(
    mcp, upstream, credentials
):
    upstream.responses["Get-Mailbox"] = [FORWARDED_RECORD]
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp, credentials, "exo_set_mailbox_forwarding", {"identity": "alice@contoso.com"}
    )

    set_call = [r for r in upstream.invoke_requests if r["cmdlet"] == "Set-Mailbox"][0]
    assert set_call["parameters"] == {
        "Identity": "alice@contoso.com",
        "ForwardingAddress": None,
        # Cleared too: forwarding configured outside this tool would otherwise
        # keep running after a "turn it off" call.
        "ForwardingSmtpAddress": None,
        "DeliverToMailboxAndForward": False,
    }
    assert result["previous_forwarding"] == {
        "forwarding_address": "contoso.onmicrosoft.com/Users/bob",
        "deliver_to_mailbox_and_forward": True,
    }


@pytest.mark.asyncio
async def test_forwarding_state_comes_from_the_read_back(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = [FORWARDED_RECORD]
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_forwarding",
        {"identity": "alice@contoso.com", "forward_to": "bob@contoso.com"},
    )

    assert result["forwarding_enabled"] is True
    assert result["forwarding"]["forwarding_address"] == "contoso.onmicrosoft.com/Users/bob"


@pytest.mark.asyncio
async def test_external_forwarding_target_is_rejected_readably(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = [MAILBOX_RECORD]
    upstream.cmdlet_errors["Set-Mailbox"] = (
        400,
        {
            "error": {
                "message": "Error executing cmdlet",
                "details": [
                    {
                        "message": "The operation couldn't be performed because object "
                        "'someone@gmail.com' couldn't be found."
                    }
                ],
            }
        },
    )

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_forwarding",
        {"identity": "alice@contoso.com", "forward_to": "someone@gmail.com"},
    )

    # Exchange's own wording reads as "that mailbox does not exist", which sends
    # the caller looking in the wrong place.
    assert result["error"]["code"] == "invalid_argument"
    assert "not a recipient in this tenant" in result["error"]["message"]
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_forwarding_an_unknown_mailbox_does_not_write(mcp, upstream, credentials):
    upstream.responses["Get-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_set_mailbox_forwarding",
        {"identity": "ghost@contoso.com", "forward_to": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "not_found"
    assert not [r for r in upstream.invoke_requests if r["cmdlet"] == "Set-Mailbox"]


# PRD-20387: mailbox/recipient permission tools.

MAILBOX_PERMISSION_RECORDS = [
    {
        "Identity": "contoso.onmicrosoft.com/Users/alice",
        "User": "NT AUTHORITY\\SELF",
        "AccessRights": ["FullAccess"],
        "Deny": False,
        "IsInherited": False,
    },
    {
        "Identity": "contoso.onmicrosoft.com/Users/alice",
        "User": "bob@contoso.com",
        "AccessRights": ["FullAccess"],
        "Deny": False,
        "IsInherited": False,
    },
    {
        "Identity": "contoso.onmicrosoft.com/Users/alice",
        "User": "carol@contoso.com",
        "AccessRights": ["FullAccess"],
        "Deny": False,
        "IsInherited": False,
    },
]

RECIPIENT_PERMISSION_RECORD = {
    "Identity": "contoso.onmicrosoft.com/Users/alice",
    "Trustee": "bob@contoso.com",
    "AccessControlType": "Allow",
    "AccessRights": ["SendAs"],
    "IsInherited": False,
}

NOT_FOUND_ERROR = (
    400,
    {
        "error": {
            "message": "Error executing cmdlet",
            "details": [
                {
                    "message": "The operation couldn't be performed because object "
                    "'ghost@contoso.com' couldn't be found."
                }
            ],
        }
    },
)


@pytest.mark.asyncio
async def test_add_mailbox_permission_grants_full_access(mcp, upstream, credentials):
    upstream.responses["Add-MailboxPermission"] = []

    result = await call(
        mcp,
        credentials,
        "exo_add_mailbox_permission",
        {"identity": "alice@contoso.com", "user": "bob@contoso.com"},
    )

    assert result == {
        "identity": "alice@contoso.com",
        "user": "bob@contoso.com",
        "access_rights": ["FullAccess"],
        "granted": True,
    }
    assert upstream.invoke_requests[0]["cmdlet"] == "Add-MailboxPermission"
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "User": "bob@contoso.com",
        "AccessRights": ["FullAccess"],
        "AutoMapping": True,
    }


@pytest.mark.asyncio
async def test_add_mailbox_permission_sends_auto_mapping_false(mcp, upstream, credentials):
    upstream.responses["Add-MailboxPermission"] = []

    await call(
        mcp,
        credentials,
        "exo_add_mailbox_permission",
        {"identity": "alice@contoso.com", "user": "bob@contoso.com", "auto_mapping": False},
    )

    assert upstream.invoke_requests[0]["parameters"]["AutoMapping"] is False


@pytest.mark.asyncio
async def test_remove_mailbox_permission_revokes_full_access(mcp, upstream, credentials):
    upstream.responses["Remove-MailboxPermission"] = []

    result = await call(
        mcp,
        credentials,
        "exo_remove_mailbox_permission",
        {"identity": "alice@contoso.com", "user": "bob@contoso.com"},
    )

    assert result == {
        "identity": "alice@contoso.com",
        "user": "bob@contoso.com",
        "access_rights": ["FullAccess"],
        "removed": True,
    }
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "User": "bob@contoso.com",
        "AccessRights": ["FullAccess"],
        "Confirm": False,
    }


@pytest.mark.asyncio
async def test_add_recipient_permission_grants_send_as(mcp, upstream, credentials):
    upstream.responses["Add-RecipientPermission"] = []

    result = await call(
        mcp,
        credentials,
        "exo_add_recipient_permission",
        {"identity": "alice@contoso.com", "trustee": "bob@contoso.com"},
    )

    assert result == {
        "identity": "alice@contoso.com",
        "trustee": "bob@contoso.com",
        "access_rights": ["SendAs"],
        "granted": True,
    }
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "Trustee": "bob@contoso.com",
        "AccessRights": ["SendAs"],
    }


@pytest.mark.asyncio
async def test_remove_recipient_permission_revokes_send_as(mcp, upstream, credentials):
    upstream.responses["Remove-RecipientPermission"] = []

    result = await call(
        mcp,
        credentials,
        "exo_remove_recipient_permission",
        {"identity": "alice@contoso.com", "trustee": "bob@contoso.com"},
    )

    assert result == {
        "identity": "alice@contoso.com",
        "trustee": "bob@contoso.com",
        "access_rights": ["SendAs"],
        "removed": True,
    }
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "Trustee": "bob@contoso.com",
        "AccessRights": ["SendAs"],
        "Confirm": False,
    }


@pytest.mark.asyncio
async def test_get_mailbox_permission_lists_trustees_excluding_self(mcp, upstream, credentials):
    upstream.responses["Get-MailboxPermission"] = MAILBOX_PERMISSION_RECORDS

    result = await call(
        mcp, credentials, "exo_get_mailbox_permission", {"identity": "alice@contoso.com"}
    )

    assert [t["user"] for t in result["trustees"]] == ["bob@contoso.com", "carol@contoso.com"]
    assert upstream.invoke_requests[0]["parameters"] == {"Identity": "alice@contoso.com"}


@pytest.mark.asyncio
async def test_get_mailbox_permission_filters_by_user(mcp, upstream, credentials):
    upstream.responses["Get-MailboxPermission"] = [MAILBOX_PERMISSION_RECORDS[1]]

    result = await call(
        mcp,
        credentials,
        "exo_get_mailbox_permission",
        {"identity": "alice@contoso.com", "user": "bob@contoso.com"},
    )

    assert [t["user"] for t in result["trustees"]] == ["bob@contoso.com"]
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "User": "bob@contoso.com",
    }


@pytest.mark.asyncio
async def test_get_mailbox_permission_empty_when_no_grant(mcp, upstream, credentials):
    upstream.responses["Get-MailboxPermission"] = []

    result = await call(
        mcp,
        credentials,
        "exo_get_mailbox_permission",
        {"identity": "alice@contoso.com", "user": "dave@contoso.com"},
    )

    assert result == {"identity": "alice@contoso.com", "trustees": []}


@pytest.mark.asyncio
async def test_get_recipient_permission_lists_trustees(mcp, upstream, credentials):
    upstream.responses["Get-RecipientPermission"] = [RECIPIENT_PERMISSION_RECORD]

    result = await call(
        mcp, credentials, "exo_get_recipient_permission", {"identity": "alice@contoso.com"}
    )

    assert result["trustees"] == [
        {
            "identity": "contoso.onmicrosoft.com/Users/alice",
            "trustee": "bob@contoso.com",
            "access_control_type": "Allow",
            "access_rights": ["SendAs"],
            "is_inherited": False,
        }
    ]
    assert upstream.invoke_requests[0]["parameters"] == {"Identity": "alice@contoso.com"}


@pytest.mark.asyncio
async def test_get_recipient_permission_filters_by_trustee(mcp, upstream, credentials):
    upstream.responses["Get-RecipientPermission"] = [RECIPIENT_PERMISSION_RECORD]

    result = await call(
        mcp,
        credentials,
        "exo_get_recipient_permission",
        {"identity": "alice@contoso.com", "trustee": "bob@contoso.com"},
    )

    assert [t["trustee"] for t in result["trustees"]] == ["bob@contoso.com"]
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "Trustee": "bob@contoso.com",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args,cmdlet",
    [
        (
            "exo_add_mailbox_permission",
            {"identity": "ghost@contoso.com", "user": "bob@contoso.com"},
            "Add-MailboxPermission",
        ),
        (
            "exo_remove_mailbox_permission",
            {"identity": "ghost@contoso.com", "user": "bob@contoso.com"},
            "Remove-MailboxPermission",
        ),
        (
            "exo_add_recipient_permission",
            {"identity": "ghost@contoso.com", "trustee": "bob@contoso.com"},
            "Add-RecipientPermission",
        ),
        (
            "exo_remove_recipient_permission",
            {"identity": "ghost@contoso.com", "trustee": "bob@contoso.com"},
            "Remove-RecipientPermission",
        ),
        (
            "exo_get_mailbox_permission",
            {"identity": "ghost@contoso.com"},
            "Get-MailboxPermission",
        ),
        (
            "exo_get_recipient_permission",
            {"identity": "ghost@contoso.com"},
            "Get-RecipientPermission",
        ),
    ],
)
async def test_permission_tools_return_not_found_for_unresolved_identity(
    mcp, upstream, credentials, tool_name, args, cmdlet
):
    upstream.cmdlet_errors[cmdlet] = NOT_FOUND_ERROR

    result = await call(mcp, credentials, tool_name, args)

    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args,cmdlet",
    [
        (
            "exo_add_mailbox_permission",
            {"identity": "alice@contoso.com", "user": "ghost@contoso.com"},
            "Add-MailboxPermission",
        ),
        (
            "exo_add_recipient_permission",
            {"identity": "alice@contoso.com", "trustee": "ghost@contoso.com"},
            "Add-RecipientPermission",
        ),
    ],
)
async def test_add_permission_tools_return_not_found_for_unresolved_trustee(
    mcp, upstream, credentials, tool_name, args, cmdlet
):
    upstream.cmdlet_errors[cmdlet] = NOT_FOUND_ERROR

    result = await call(mcp, credentials, tool_name, args)

    assert result["error"]["code"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("exo_add_mailbox_permission", {"identity": "a", "user": "b"}),
        ("exo_remove_mailbox_permission", {"identity": "a", "user": "b"}),
        ("exo_add_recipient_permission", {"identity": "a", "trustee": "b"}),
        ("exo_remove_recipient_permission", {"identity": "a", "trustee": "b"}),
        ("exo_get_mailbox_permission", {"identity": "a"}),
        ("exo_get_recipient_permission", {"identity": "a"}),
    ],
)
async def test_permission_tools_without_credentials_return_not_configured(
    mcp, tool_name, args
):
    # No contextvar set -> client_factory() resolves to None, same as the
    # existing 7 tools outside a request context.
    result = tool_json(await mcp.call_tool(tool_name, args))
    assert result["error"]["code"] == "not_configured"


@pytest.mark.asyncio
async def test_permission_tools_isolate_concurrent_tenants(mcp, upstream, credentials):
    other_credentials = ExoCredentials(
        tenant_id="fabrikam.onmicrosoft.com",
        client_id=CLIENT_ID,
        certificate_b64=credentials.certificate_b64,
    )
    upstream.responses["Get-MailboxPermission"] = [MAILBOX_PERMISSION_RECORDS[1]]

    result_a, result_b = await asyncio.gather(
        call(mcp, credentials, "exo_get_mailbox_permission", {"identity": "alice@contoso.com"}),
        call(
            mcp,
            other_credentials,
            "exo_get_mailbox_permission",
            {"identity": "alice@contoso.com"},
        ),
    )

    assert "error" not in result_a
    assert "error" not in result_b

    invoke_urls = {req["url"] for req in upstream.invoke_requests}
    assert any(f"/{TENANT}/InvokeCommand" in url for url in invoke_urls)
    assert any("/fabrikam.onmicrosoft.com/InvokeCommand" in url for url in invoke_urls)
    assert len(invoke_urls) == 2


# ---------------------------------------------------------------------------
# PRD-20480: exo_add_sendonbehalf_permission, exo_add_email_alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sendonbehalf_permission_grants_delegate(mcp, upstream, credentials):
    """TC-EXO-001/AC3: grants SendOnBehalf via an additive Set-Mailbox call,
    and the response is reported unverified pending EXO replication."""
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_add_sendonbehalf_permission",
        {"identity": "shared@contoso.com", "delegate": "bob@contoso.com"},
    )

    assert result["identity"] == "shared@contoso.com"
    assert result["delegate"] == "bob@contoso.com"
    assert result["granted"] is True
    assert result["verified"] is False
    assert "exo_get_mailbox" in result["note"]
    assert upstream.invoke_requests[0]["cmdlet"] == "Set-Mailbox"
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "shared@contoso.com",
        "GrantSendOnBehalfTo": {"Add": ["bob@contoso.com"]},
    }


@pytest.mark.asyncio
async def test_add_sendonbehalf_permission_not_found(mcp, upstream, credentials):
    upstream.cmdlet_errors["Set-Mailbox"] = NOT_FOUND_ERROR

    result = await call(
        mcp,
        credentials,
        "exo_add_sendonbehalf_permission",
        {"identity": "ghost@contoso.com", "delegate": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_add_email_alias_adds_multiple_without_disturbing_existing(
    mcp, upstream, credentials
):
    """TC-EXO-002/AC4: two new alias addresses in one call must both be sent
    as an additive Add (never a full replace of EmailAddresses)."""
    upstream.responses["Set-Mailbox"] = []

    result = await call(
        mcp,
        credentials,
        "exo_add_email_alias",
        {
            "identity": "alice@contoso.com",
            "aliases": ["old-name@contoso.com", "alias2@contoso.com"],
        },
    )

    assert result["identity"] == "alice@contoso.com"
    assert result["aliases_requested"] == ["old-name@contoso.com", "alias2@contoso.com"]
    assert result["accepted"] is True
    assert result["verified"] is False
    assert upstream.invoke_requests[0]["cmdlet"] == "Set-Mailbox"
    assert upstream.invoke_requests[0]["parameters"] == {
        "Identity": "alice@contoso.com",
        "EmailAddresses": {"Add": ["smtp:old-name@contoso.com", "smtp:alias2@contoso.com"]},
    }


@pytest.mark.asyncio
async def test_add_email_alias_rejects_address_in_use_elsewhere(mcp, upstream, credentials):
    """TC-EXO-003/AC5: an alias already assigned to a different recipient must
    surface Exchange's rejection, not be silently dropped."""
    upstream.cmdlet_errors["Set-Mailbox"] = (
        400,
        {
            "error": {
                "message": "Error executing cmdlet",
                "details": [
                    {
                        "message": 'The proxy address "smtp:taken@contoso.com" is already '
                        'being used by the recipient "bob@contoso.com". Please choose '
                        "another proxy address."
                    }
                ],
            }
        },
    )

    result = await call(
        mcp,
        credentials,
        "exo_add_email_alias",
        {"identity": "alice@contoso.com", "aliases": ["taken@contoso.com"]},
    )

    assert result["error"]["code"] == "invalid_argument"
    assert "taken@contoso.com" in result["error"]["message"]


@pytest.mark.asyncio
async def test_add_email_alias_rejects_empty_list_without_calling_exchange(
    mcp, upstream, credentials
):
    result = await call(
        mcp, credentials, "exo_add_email_alias", {"identity": "alice@contoso.com", "aliases": []}
    )

    assert result["error"]["code"] == "invalid_argument"
    assert upstream.invoke_requests == []


@pytest.mark.asyncio
async def test_add_email_alias_not_found(mcp, upstream, credentials):
    upstream.cmdlet_errors["Set-Mailbox"] = NOT_FOUND_ERROR

    result = await call(
        mcp,
        credentials,
        "exo_add_email_alias",
        {"identity": "ghost@contoso.com", "aliases": ["x@contoso.com"]},
    )

    assert result["error"]["code"] == "not_found"
