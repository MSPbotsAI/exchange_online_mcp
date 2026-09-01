from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import ExoClient, ExoError
from ._common import (
    NO_CREDENTIALS,
    SHARED_MAILBOX_LIMIT_BYTES,
    parse_size_bytes,
    project,
)

_MAILBOX_FIELDS = {
    "Identity": "identity",
    "UserPrincipalName": "user_principal_name",
    "PrimarySmtpAddress": "primary_smtp_address",
    "DisplayName": "display_name",
    "RecipientTypeDetails": "recipient_type_details",
    "HiddenFromAddressListsEnabled": "hidden_from_address_lists_enabled",
    "LitigationHoldEnabled": "litigation_hold_enabled",
    "InPlaceHolds": "in_place_holds",
    "ArchiveStatus": "archive_status",
    "AccountDisabled": "account_disabled",
    "ForwardingSmtpAddress": "forwarding_smtp_address",
    "ForwardingAddress": "forwarding_address",
    "DeliverToMailboxAndForward": "deliver_to_mailbox_and_forward",
    "ProhibitSendReceiveQuota": "prohibit_send_receive_quota",
    "WhenMailboxCreated": "when_mailbox_created",
}

_STATISTICS_FIELDS = {
    "TotalItemSize": "total_item_size",
    "ItemCount": "item_count",
    "TotalDeletedItemSize": "total_deleted_item_size",
    "LastLogonTime": "last_logon_time",
}


async def _read_mailbox(client: ExoClient, identity: str) -> dict | None:
    records = await client.invoke("Get-Mailbox", {"Identity": identity})
    if not records:
        return None
    return project(records[0], _MAILBOX_FIELDS)


def _shared_conversion(mailbox: dict, statistics: dict | None) -> dict:
    """Whether this mailbox can become a licence-free shared mailbox.

    A shared mailbox needs no licence only below 50 GB and without an archive
    or a hold, so the caller has to measure before converting.
    """
    size_bytes = parse_size_bytes((statistics or {}).get("total_item_size"))
    archive_status = str(mailbox.get("archive_status") or "None")
    archive_enabled = archive_status.lower() not in ("none", "")
    litigation_hold = bool(mailbox.get("litigation_hold_enabled"))
    in_place_hold = bool(mailbox.get("in_place_holds"))
    over_limit = size_bytes is not None and size_bytes > SHARED_MAILBOX_LIMIT_BYTES

    result = {
        "limit_gb": 50,
        "over_limit": over_limit,
        "archive_enabled": archive_enabled,
        "litigation_hold_enabled": litigation_hold,
        "in_place_hold": in_place_hold,
        "license_required": over_limit or archive_enabled or litigation_hold or in_place_hold,
    }
    if size_bytes is None:
        result["size_unknown"] = True
    else:
        result["size_bytes"] = size_bytes
        result["size_gb"] = round(size_bytes / 1024**3, 2)
    return result


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_get_mailbox(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        include_statistics: Annotated[
            bool,
            Field(description="Include size and last-logon statistics."),
        ] = True,
    ) -> str:
        """Get a mailbox with its type, GAL visibility, size and hold state.

        Read this before converting a mailbox: shared_conversion tells you
        whether it can go licence-free as a shared mailbox (under 50 GB, no
        archive, no hold). Covers user and shared mailboxes.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            mailbox = await _read_mailbox(client, identity)
            if mailbox is None:
                return ExoError(404, f"No mailbox found for '{identity}'").to_envelope()

            result: dict = {"mailbox": mailbox}
            statistics: dict | None = None
            if include_statistics:
                try:
                    records = await client.invoke(
                        "Get-MailboxStatistics", {"Identity": identity}
                    )
                    statistics = project(records[0], _STATISTICS_FIELDS) if records else {}
                    result["statistics"] = statistics
                except ExoError as exc:
                    # A mailbox that has never been logged into has no statistics.
                    # The mailbox data is still useful, so report and continue.
                    result["statistics"] = None
                    result["statistics_error"] = exc.message
            result["shared_conversion"] = _shared_conversion(mailbox, statistics)
            return dump_json_capped(result)
        except ExoError as exc:
            return exc.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
    async def exo_convert_mailbox_to_shared(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
    ) -> str:
        """Convert a user mailbox into a shared mailbox.

        Colleagues keep access to the mail history and the mailbox stops
        consuming a licence — but only below 50 GB and without an archive or
        hold, so call exo_get_mailbox first and check
        shared_conversion.license_required. Converting back is a separate
        manual operation. Already-shared mailboxes are left unchanged.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke("Set-Mailbox", {"Identity": identity, "Type": "Shared"})
        except ExoError as exc:
            return exc.to_envelope()

        result = {"identity": identity, "converted": True}
        try:
            mailbox = await _read_mailbox(client, identity)
        except ExoError:
            mailbox = None
        if mailbox:
            result["recipient_type_details"] = mailbox.get("recipient_type_details")
        else:
            # The conversion itself succeeded; only the read-back failed.
            result["verified"] = False
        return dump_json_capped(result)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def exo_set_mailbox_hidden(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        hidden: Annotated[
            bool,
            Field(description="True hides the mailbox from address lists, false unhides it."),
        ],
    ) -> str:
        """Hide or unhide a mailbox in the global address list.

        The mailbox stops appearing in the directory but stays reachable at its
        address. Reversible: call again with hidden=false.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Set-Mailbox",
                {"Identity": identity, "HiddenFromAddressListsEnabled": hidden},
            )
        except ExoError as exc:
            return exc.to_envelope()

        result = {"identity": identity, "hidden_from_address_lists_enabled": hidden}
        try:
            mailbox = await _read_mailbox(client, identity)
        except ExoError:
            mailbox = None
        if mailbox and "hidden_from_address_lists_enabled" in mailbox:
            result["hidden_from_address_lists_enabled"] = mailbox[
                "hidden_from_address_lists_enabled"
            ]
        elif not mailbox:
            result["verified"] = False
        return dump_json_capped(result)
