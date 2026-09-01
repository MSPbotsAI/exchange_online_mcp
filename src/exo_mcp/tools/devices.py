from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import ExoClient, ExoError
from ._common import NO_CREDENTIALS, project

_MAX_LIMIT = 200

_DEVICE_FIELDS = {
    "Guid": "guid",
    "Identity": "identity",
    "DeviceId": "device_id",
    "DeviceType": "device_type",
    "DeviceModel": "device_model",
    "DeviceOS": "device_os",
    "DeviceUserAgent": "device_user_agent",
    "FriendlyName": "friendly_name",
    "ClientType": "client_type",
    "DeviceAccessState": "device_access_state",
    "FirstSyncTime": "first_sync_time",
    "UserDisplayName": "user_display_name",
}


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_list_mobile_devices(
        mailbox: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        limit: Annotated[
            int,
            Field(description="Maximum devices to return, 1-200.", ge=1, le=_MAX_LIMIT),
        ] = 50,
    ) -> str:
        """List the mobile devices associated with a mailbox.

        These are the Exchange ActiveSync device partnerships shown in the
        Exchange admin center — the same list a directory or device-management
        API will not give you. A mailbox with no devices returns an empty list.
        Pass guid to exo_remove_mobile_device to drop one.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        capped = max(1, min(limit, _MAX_LIMIT))
        try:
            records = await client.invoke(
                "Get-MobileDevice", {"Mailbox": mailbox}, max_page_size=capped
            )
        except ExoError as exc:
            return exc.to_envelope()

        devices = [project(record, _DEVICE_FIELDS) for record in records[:capped]]
        return dump_json_capped(
            {
                "mailbox": mailbox,
                "devices": devices,
                "count": len(devices),
                "has_more": len(records) > capped,
            }
        )

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def exo_remove_mobile_device(
        device_id: Annotated[
            str,
            Field(
                description=(
                    "Device guid or identity as returned by exo_list_mobile_devices."
                )
            ),
        ],
    ) -> str:
        """Remove one mobile device partnership from its mailbox.

        The device can no longer sync the mailbox and disappears from the
        Exchange admin center. This does NOT wipe company data already on the
        handset — that is a separate wipe operation this server does not
        expose. Takes a single device id; there is no bulk removal.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Remove-MobileDevice", {"Identity": device_id, "Confirm": False}
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped({"device_id": device_id, "removed": True})
