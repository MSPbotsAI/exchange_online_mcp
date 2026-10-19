from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import ExoClient, ExoError
from ._common import NO_CREDENTIALS, contains_case_insensitive, project
from .mailbox import _read_mailbox

# PRD-20480: EXO replication of a Set-Mailbox write (GrantSendOnBehalfTo) can
# take several minutes, same caveat as mailbox.py's Set-Mailbox tools — so the
# response is reported accepted-but-unverified, confirm later with
# exo_get_mailbox.
_SENDONBEHALF_PENDING_NOTE = (
    "Exchange Online accepted the change, but it replicates across the "
    "directory, which commonly takes a few minutes. Confirm with "
    "exo_get_mailbox afterwards — until replication completes, "
    "GrantSendOnBehalfTo may still show the previous value."
)

# PRD-20387: 6 mailbox/recipient permission tools, fixed-right wrappers around
# the matching Add-/Remove-/Get- cmdlets. Access rights are deliberately not
# caller-settable — FullAccess for mailbox permission, SendAs for recipient
# permission — matching the ticket's Minimal Viable Slice.

_MAILBOX_PERMISSION_FIELDS = {
    "Identity": "identity",
    "User": "user",
    "AccessRights": "access_rights",
    "Deny": "deny",
    "IsInherited": "is_inherited",
}

_RECIPIENT_PERMISSION_FIELDS = {
    "Identity": "identity",
    "Trustee": "trustee",
    "AccessControlType": "access_control_type",
    "AccessRights": "access_rights",
    "IsInherited": "is_inherited",
}

# PRD-20387 open question (assumed, non-blocking): the mailbox owner's own
# implicit self-grant is never an actionable trustee for this feature's
# purpose, so it is dropped from the two read tools' output -- but only when
# the caller did not ask about a specific user/trustee explicitly.
_SELF_TRUSTEE = "nt authority\\self"


def _is_self(user_or_trustee: object) -> bool:
    return isinstance(user_or_trustee, str) and user_or_trustee.strip().lower() == _SELF_TRUSTEE


def _rights(record: dict) -> list[str]:
    """AccessRights as a list regardless of whether Exchange projected it as a
    scalar string or a multi-valued array."""
    value = record.get("access_rights")
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=False))
    async def exo_add_mailbox_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        user: Annotated[
            str,
            Field(description="Trustee to grant FullAccess to: UPN, email address, "
                              "alias or GUID."),
        ],
        auto_mapping: Annotated[
            bool,
            Field(description="Whether the trustee's Outlook profile auto-adds this "
                              "mailbox. False grants access without the mailbox "
                              "appearing automatically."),
        ] = True,
    ) -> str:
        """Grant a trustee FullAccess on a mailbox.

        Access right is fixed to FullAccess -- this tool does not accept other
        rights. Errors if the trustee already has FullAccess; call
        exo_get_mailbox_permission first to check current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Add-MailboxPermission",
                {
                    "Identity": identity,
                    "User": user,
                    "AccessRights": ["FullAccess"],
                    "AutoMapping": auto_mapping,
                },
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped(
            {"identity": identity, "user": user, "access_rights": ["FullAccess"], "granted": True}
        )

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def exo_add_sendonbehalf_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        delegate: Annotated[
            str,
            Field(description="Trustee to grant SendOnBehalf to: UPN, email address, "
                              "alias or GUID. Must already exist as a recipient in "
                              "this tenant."),
        ],
    ) -> str:
        """Grant a trustee SendOnBehalf on a mailbox (PRD-20480).

        Different right from exo_add_mailbox_permission's FullAccess: mail
        shows as sent "on behalf of" the mailbox, not full access to it —
        the shared-mailbox "send on behalf" case. Additive: existing
        delegates are kept (read-merge-write, not a raw Add). Delegate must
        already be a recipient; already-granted reports already_present
        instead of erroring. Write is confirmed with a read-back.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            mailbox = await _read_mailbox(client, identity)
        except ExoError as exc:
            return exc.to_envelope()
        if mailbox is None:
            return ExoError(
                404, f"No mailbox found for '{identity}'", code="not_found"
            ).to_envelope()

        # D2: validate the delegate exists before writing, instead of
        # letting Exchange reject the whole write with an undifferentiated
        # "Invalid Operation".
        try:
            recipients = await client.invoke("Get-Recipient", {"Identity": delegate})
        except ExoError as exc:
            return exc.to_envelope()
        if not recipients:
            return error_envelope(
                "invalid_argument",
                f"'{delegate}' is not a recipient in this tenant; SendOnBehalf can "
                "only be granted to an existing mailbox, mail user or mail-enabled "
                "group.",
                False,
            )
        delegate_record = recipients[0]
        delegate_identity = delegate_record.get("Identity")
        delegate_label = delegate_record.get("PrimarySmtpAddress") or delegate_identity

        current = mailbox.get("grant_send_on_behalf_to") or []
        already_present = (
            contains_case_insensitive(current, delegate)
            or (delegate_identity and contains_case_insensitive(current, str(delegate_identity)))
            or (delegate_label and contains_case_insensitive(current, str(delegate_label)))
        )
        if already_present:
            return dump_json_capped(
                {
                    "identity": identity,
                    "delegate": delegate,
                    "result": "already_present",
                    "granted": True,
                }
            )

        merged = [*current, delegate]
        try:
            await client.invoke(
                "Set-Mailbox", {"Identity": identity, "GrantSendOnBehalfTo": merged}
            )
        except ExoError as exc:
            return exc.to_envelope()

        verified = False
        try:
            after = await _read_mailbox(client, identity)
        except ExoError:
            after = None
        if after is not None:
            after_values = after.get("grant_send_on_behalf_to") or []
            verified = contains_case_insensitive(after_values, delegate) or (
                delegate_label is not None
                and contains_case_insensitive(after_values, str(delegate_label))
            )

        result: dict = {
            "identity": identity,
            "delegate": delegate,
            "result": "added",
            "granted": True,
            "verified": verified,
        }
        if not verified:
            result["note"] = _SENDONBEHALF_PENDING_NOTE
        return dump_json_capped(result)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def exo_remove_mailbox_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        user: Annotated[
            str,
            Field(description="Trustee to revoke FullAccess from: UPN, email address, "
                              "alias or GUID."),
        ],
    ) -> str:
        """Revoke a trustee's FullAccess on a mailbox.

        Access right is fixed to FullAccess. Errors if the trustee does not
        currently have FullAccess; call exo_get_mailbox_permission first to
        check current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Remove-MailboxPermission",
                {
                    "Identity": identity,
                    "User": user,
                    "AccessRights": ["FullAccess"],
                    "Confirm": False,
                },
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped(
            {"identity": identity, "user": user, "access_rights": ["FullAccess"], "removed": True}
        )

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=False))
    async def exo_add_recipient_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        trustee: Annotated[
            str,
            Field(description="Trustee to grant SendAs to: UPN, email address, alias "
                              "or GUID."),
        ],
    ) -> str:
        """Grant a trustee SendAs on a mailbox.

        Access right is fixed to SendAs -- this tool does not accept other
        rights. Errors if the trustee already has SendAs; call
        exo_get_recipient_permission first to check current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Add-RecipientPermission",
                {"Identity": identity, "Trustee": trustee, "AccessRights": ["SendAs"]},
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped(
            {
                "identity": identity,
                "trustee": trustee,
                "access_rights": ["SendAs"],
                "granted": True,
            }
        )

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def exo_remove_recipient_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        trustee: Annotated[
            str,
            Field(description="Trustee to revoke SendAs from: UPN, email address, "
                              "alias or GUID."),
        ],
    ) -> str:
        """Revoke a trustee's SendAs on a mailbox.

        Access right is fixed to SendAs. Errors if the trustee does not
        currently have SendAs; call exo_get_recipient_permission first to
        check current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            await client.invoke(
                "Remove-RecipientPermission",
                {
                    "Identity": identity,
                    "Trustee": trustee,
                    "AccessRights": ["SendAs"],
                    "Confirm": False,
                },
            )
        except ExoError as exc:
            return exc.to_envelope()

        return dump_json_capped(
            {
                "identity": identity,
                "trustee": trustee,
                "access_rights": ["SendAs"],
                "removed": True,
            }
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_get_mailbox_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        user: Annotated[
            str | None,
            Field(description="Narrow to one trustee's FullAccess grant. Omit to list "
                              "every trustee with FullAccess on this mailbox."),
        ] = None,
    ) -> str:
        """List who holds FullAccess on a mailbox, or check one trustee.

        Only FullAccess grants are reported -- Exchange can return other right
        types from the same cmdlet, and the mailbox owner's own implicit
        self-grant is excluded when listing everyone. An empty list means no
        matching grant, not an error. Call before
        exo_add_mailbox_permission/exo_remove_mailbox_permission to see
        current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            records = await client.invoke(
                "Get-MailboxPermission", {"Identity": identity, "User": user}
            )
        except ExoError as exc:
            return exc.to_envelope()

        trustees = []
        for record in records:
            projected = project(record, _MAILBOX_PERMISSION_FIELDS)
            if "FullAccess" not in _rights(projected):
                continue
            if user is None and _is_self(projected.get("user")):
                continue
            trustees.append(projected)

        return dump_json_capped({"identity": identity, "trustees": trustees})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_get_recipient_permission(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        trustee: Annotated[
            str | None,
            Field(description="Narrow to one trustee's SendAs grant. Omit to list "
                              "every trustee with SendAs on this mailbox."),
        ] = None,
    ) -> str:
        """List who holds SendAs on a mailbox, or check one trustee.

        Only SendAs grants are reported -- Exchange can return other right
        types from the same cmdlet, and the mailbox owner's own implicit
        self-grant is excluded when listing everyone. An empty list means no
        matching grant, not an error. Call before
        exo_add_recipient_permission/exo_remove_recipient_permission to see
        current state.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            records = await client.invoke(
                "Get-RecipientPermission", {"Identity": identity, "Trustee": trustee}
            )
        except ExoError as exc:
            return exc.to_envelope()

        trustees = []
        for record in records:
            projected = project(record, _RECIPIENT_PERMISSION_FIELDS)
            if "SendAs" not in _rights(projected):
                continue
            if trustee is None and _is_self(projected.get("trustee")):
                continue
            trustees.append(projected)

        return dump_json_capped({"identity": identity, "trustees": trustees})
