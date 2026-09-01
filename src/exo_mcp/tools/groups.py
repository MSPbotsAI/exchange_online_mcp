from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import ExoClient, ExoError
from ._common import NO_CREDENTIALS


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def exo_remove_distribution_group_member(
        group: Annotated[
            str,
            Field(
                description=(
                    "Distribution group or mail-enabled security group: name, "
                    "email address or GUID."
                )
            ),
        ],
        member: Annotated[
            str,
            Field(description="Member to remove: UPN, email address, alias or GUID."),
        ],
        bypass_security_group_manager_check: Annotated[
            bool,
            Field(
                description=(
                    "Allow the removal when the caller is not listed as a group "
                    "manager. Needed for most admin-driven removals."
                )
            ),
        ] = True,
    ) -> str:
        """Remove one member from a distribution or mail-enabled security group.

        These two group types are owned by Exchange, so directory APIs cannot
        change their membership. Microsoft 365 groups and plain security groups
        are not handled here — use Microsoft Graph for those. Removes a single
        named member only; the group and other members are untouched.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Remove-DistributionGroupMember",
                {
                    "Identity": group,
                    "Member": member,
                    "BypassSecurityGroupManagerCheck": bypass_security_group_manager_check,
                    "Confirm": False,
                },
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped({"group": group, "member": member, "removed": True})
