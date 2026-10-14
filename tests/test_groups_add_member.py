"""exo_add_distribution_group_member: idempotency and the three restricted
group types (PRD-20394)."""

import pytest
from conftest import tool_json

from exo_mcp.config import Settings
from exo_mcp.server import _credentials_var, create_mcp_server

# A Get-Recipient record for a usable (plain) distribution group.
USABLE_GROUP = {
    "Identity": "sales@contoso.com",
    "RecipientTypeDetails": "MailUniversalDistributionGroup",
    "IsDirSynced": False,
}

EXISTING_MEMBER = {
    "Name": "Alice Smith",
    "PrimarySmtpAddress": "alice@contoso.com",
    "Alias": "alice",
    "Guid": "11111111-0000-0000-0000-000000000000",
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
async def test_add_new_member_succeeds(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result == {"group": "sales@contoso.com", "member": "bob@contoso.com", "added": True}
    add_calls = [
        r for r in upstream.invoke_requests if r["cmdlet"] == "Add-DistributionGroupMember"
    ]
    assert len(add_calls) == 1
    # BypassSecurityGroupManagerCheck is omitted by default — sending it at all
    # needs Organization Management (PRD-20394 INT finding).
    assert add_calls[0]["parameters"] == {
        "Identity": "sales@contoso.com",
        "Member": "bob@contoso.com",
        "Confirm": False,
    }


@pytest.mark.asyncio
async def test_add_existing_member_is_idempotent(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "alice@contoso.com"},
    )

    assert result == {
        "group": "sales@contoso.com",
        "member": "alice@contoso.com",
        "added": False,
        "already_member": True,
    }
    assert not [r for r in upstream.invoke_requests if r["cmdlet"] == "Add-DistributionGroupMember"]


@pytest.mark.asyncio
async def test_add_existing_member_matches_case_insensitively(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "ALICE@CONTOSO.COM"},
    )

    assert result["already_member"] is True


@pytest.mark.asyncio
async def test_add_member_rejects_dynamic_distribution_group(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [
        {**USABLE_GROUP, "RecipientTypeDetails": "DynamicDistributionGroup"}
    ]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "dynamic_group_unsupported"
    assert "dynamic distribution group" in result["error"]["message"]
    assert not [r for r in upstream.invoke_requests if r["cmdlet"] == "Get-DistributionGroupMember"]


@pytest.mark.asyncio
async def test_add_member_rejects_directory_synced_group(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [{**USABLE_GROUP, "IsDirSynced": True}]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "directory_synced_group_unsupported"
    assert "on-premises" in result["error"]["message"]


@pytest.mark.asyncio
async def test_add_member_rejects_microsoft_365_group(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [{**USABLE_GROUP, "RecipientTypeDetails": "GroupMailbox"}]

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "microsoft_365_group_unsupported"
    assert "Microsoft 365 Group" in result["error"]["message"]


@pytest.mark.asyncio
async def test_restricted_group_error_codes_are_distinct(mcp, upstream, credentials):
    codes = set()
    for recipient_type, synced in (
        ("DynamicDistributionGroup", False),
        ("MailUniversalDistributionGroup", True),
        ("GroupMailbox", False),
    ):
        upstream.responses["Get-Recipient"] = [
            {**USABLE_GROUP, "RecipientTypeDetails": recipient_type, "IsDirSynced": synced}
        ]
        result = await call(
            mcp,
            credentials,
            "exo_add_distribution_group_member",
            {"group": "sales@contoso.com", "member": "bob@contoso.com"},
        )
        codes.add(result["error"]["code"])

    assert codes == {
        "dynamic_group_unsupported",
        "directory_synced_group_unsupported",
        "microsoft_365_group_unsupported",
    }


@pytest.mark.asyncio
async def test_add_member_group_not_found(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = []

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "ghost@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "not_found"
    assert not [r for r in upstream.invoke_requests if r["cmdlet"] == "Get-DistributionGroupMember"]


@pytest.mark.asyncio
async def test_add_member_member_not_found(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]
    upstream.cmdlet_errors["Add-DistributionGroupMember"] = (
        400,
        {"error": {"message": "Couldn't find object \"ghost@contoso.com\"."}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "ghost@contoso.com"},
    )

    assert result["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_add_member_sends_bypass_only_when_asked(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]

    await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {
            "group": "sales@contoso.com",
            "member": "bob@contoso.com",
            "bypass_security_group_manager_check": True,
        },
    )

    add_call = next(
        r for r in upstream.invoke_requests if r["cmdlet"] == "Add-DistributionGroupMember"
    )
    assert add_call["parameters"]["BypassSecurityGroupManagerCheck"] is True


@pytest.mark.asyncio
async def test_add_member_explicit_false_omits_bypass(mcp, upstream, credentials):
    """False must not reach the wire: Exchange checks role entries per
    parameter, so even `BypassSecurityGroupManagerCheck: false` is refused for
    a Recipient Management app (observed on INT, 2026-10-14)."""
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]

    await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {
            "group": "sales@contoso.com",
            "member": "bob@contoso.com",
            "bypass_security_group_manager_check": False,
        },
    )

    add_call = next(
        r for r in upstream.invoke_requests if r["cmdlet"] == "Add-DistributionGroupMember"
    )
    assert "BypassSecurityGroupManagerCheck" not in add_call["parameters"]


@pytest.mark.asyncio
async def test_add_member_bypass_denied_maps_to_named_error(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]
    upstream.cmdlet_errors["Add-DistributionGroupMember"] = (
        403,
        {"error": {"message": "Access denied"}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {
            "group": "sales@contoso.com",
            "member": "bob@contoso.com",
            "bypass_security_group_manager_check": True,
        },
    )

    assert result["error"]["code"] == "bypass_not_permitted"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_add_member_rbac_denial_without_bypass_stays_unauthorized(
    mcp, upstream, credentials
):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]
    upstream.cmdlet_errors["Add-DistributionGroupMember"] = (
        403,
        {"error": {"message": "Access denied"}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_add_member_not_owner_maps_to_named_error(mcp, upstream, credentials):
    upstream.responses["Get-Recipient"] = [USABLE_GROUP]
    upstream.responses["Get-DistributionGroupMember"] = [EXISTING_MEMBER]
    upstream.cmdlet_errors["Add-DistributionGroupMember"] = (
        400,
        {
            "error": {
                "message": "You don't have sufficient permissions. This operation can "
                "only be performed by a manager of the group."
            }
        },
    )

    result = await call(
        mcp,
        credentials,
        "exo_add_distribution_group_member",
        {"group": "sales@contoso.com", "member": "bob@contoso.com"},
    )

    assert result["error"]["code"] == "not_group_manager"


@pytest.mark.asyncio
async def test_remove_member_omits_bypass_by_default_and_maps_denial(
    mcp, upstream, credentials
):
    upstream.cmdlet_errors["Remove-DistributionGroupMember"] = (
        403,
        {"error": {"message": "Access denied"}},
    )

    result = await call(
        mcp,
        credentials,
        "exo_remove_distribution_group_member",
        {
            "group": "sales@contoso.com",
            "member": "alice@contoso.com",
            "bypass_security_group_manager_check": True,
        },
    )

    assert result["error"]["code"] == "bypass_not_permitted"
    assert upstream.invoke_requests[0]["parameters"]["BypassSecurityGroupManagerCheck"] is True
