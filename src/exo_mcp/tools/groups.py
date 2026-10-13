from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import ExoClient, ExoError
from ._common import NO_CREDENTIALS, classify_group_restriction, project

_MEMBER_FIELDS = {
    "Name": "name",
    "PrimarySmtpAddress": "primary_smtp_address",
    "Alias": "alias",
    "Guid": "guid",
}

# PRD-20394: Get-DistributionGroupMember pagination follows devices.py's
# Get-MobileDevice pattern (max_page_size cap, same shape of Prefer header).
_MEMBER_PAGE_SIZE = 200

# No confirmed upstream wording exists for "member already exists" (see
# docs/tickets/PRD-20394.md § Tech design, Risks & dependencies #2) — this is
# a best-effort fallback for the TOCTOU race between the membership precheck
# and the write, not the primary idempotency mechanism (that is the precheck
# itself, below). A race that surfaces different wording falls through to a
# plain upstream_error instead of being mistaken for something else.
_DUPLICATE_MEMBER_MARKERS = ("already a member", "already exists")


def _is_already_member(member: str, members: list[dict]) -> bool:
    needle = member.strip().lower()
    for record in members:
        candidates = (
            record.get("name"),
            record.get("primary_smtp_address"),
            record.get("alias"),
            record.get("guid"),
        )
        if any(isinstance(c, str) and c.strip().lower() == needle for c in candidates):
            return True
    return False


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
    async def exo_add_distribution_group_member(
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
            Field(description="Member to add: UPN, email address, alias or GUID."),
        ],
        bypass_security_group_manager_check: Annotated[
            bool,
            Field(
                description=(
                    "Allow the addition when the caller is not listed as a group "
                    "manager. Needed for most admin-driven additions."
                )
            ),
        ] = True,
    ) -> str:
        """Add one member to a distribution group or mail-enabled security group.

        Safe to call repeatedly: a member already present is reported as
        already_member rather than an error. Dynamic distribution groups,
        on-premises-synchronized groups and Microsoft 365 Groups are rejected
        with a distinct named error for each — none of them can be managed
        through this tool. Adds a single named member only.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            recipients = await client.invoke("Get-Recipient", {"Identity": group})
        except ExoError as exc:
            return exc.to_envelope()
        if not recipients:
            return ExoError(404, f"No recipient found for '{group}'", code="not_found").to_envelope()

        recipient = recipients[0]
        restriction = classify_group_restriction(
            recipient.get("RecipientTypeDetails"), bool(recipient.get("IsDirSynced"))
        )
        if restriction is not None:
            code, message = restriction
            return ExoError(
                400, f"'{group}' {message}", code=code, retryable=False
            ).to_envelope()

        try:
            member_records = await client.invoke(
                "Get-DistributionGroupMember",
                {"Identity": group},
                max_page_size=_MEMBER_PAGE_SIZE,
            )
        except ExoError as exc:
            return exc.to_envelope()
        members = [project(record, _MEMBER_FIELDS) for record in member_records]

        if _is_already_member(member, members):
            return dump_json_capped(
                {"group": group, "member": member, "added": False, "already_member": True}
            )

        try:
            await client.invoke(
                "Add-DistributionGroupMember",
                {
                    "Identity": group,
                    "Member": member,
                    "BypassSecurityGroupManagerCheck": bypass_security_group_manager_check,
                    "Confirm": False,
                },
            )
        except ExoError as exc:
            if any(marker in exc.message.lower() for marker in _DUPLICATE_MEMBER_MARKERS):
                return dump_json_capped(
                    {"group": group, "member": member, "added": False, "already_member": True}
                )
            return exc.to_envelope()

        return dump_json_capped({"group": group, "member": member, "added": True})

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
