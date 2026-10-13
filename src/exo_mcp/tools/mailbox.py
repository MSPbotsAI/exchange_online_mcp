from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import CLEAR, ExoClient, ExoError
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


_FORWARDING_FIELDS = (
    "forwarding_address",
    "forwarding_smtp_address",
    "deliver_to_mailbox_and_forward",
)


def _forwarding_state(mailbox: dict) -> dict:
    """The forwarding-related subset of a projected mailbox."""
    return {key: mailbox[key] for key in _FORWARDING_FIELDS if key in mailbox}


_AUTO_REPLY_FIELDS = {
    "AutoReplyState": "auto_reply_state",
    "ExternalAudience": "external_audience",
    "InternalMessage": "internal_message",
    "ExternalMessage": "external_message",
    "StartTime": "start_time",
    "EndTime": "end_time",
}

_AUTO_REPLY_STATES = {"Disabled", "Enabled", "Scheduled"}
_EXTERNAL_AUDIENCES = {"None", "Known", "All"}


def _datetime_to_z(value: datetime) -> str:
    """Render an aware datetime as UTC ISO-8601 with a trailing Z."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_upstream_time(value: object) -> object:
    """Best-effort: render an upstream auto-reply time as UTC ISO-8601 + Z.

    PRD-20394: the exact wire format `Get-MailboxAutoReplyConfiguration`
    returns over this server's REST path (as opposed to interactive
    PowerShell) is not confirmed empirically — see docs/tickets/PRD-20394.md
    § Tech design, Risks & dependencies #1. This assumes UTC storage and a
    parseable ISO-8601-like string; a value that does not parse is passed
    through unchanged rather than raising, since a read path should degrade
    gracefully on an unexpected shape, not break the whole response.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _datetime_to_z(parsed)


def _parse_offset_datetime(value: str, field_name: str) -> datetime:
    """Parse a caller-supplied date/time, requiring a timezone offset.

    Raises ExoError(code="invalid_argument") rather than assuming UTC or
    local time on an unparsable or offset-less value — the ticket requires
    this to be rejected explicitly, never silently guessed (AC10).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExoError(
            400,
            f"{field_name} is not a valid ISO-8601 date/time: '{value}'",
            code="invalid_argument",
            retryable=False,
        ) from exc
    if parsed.utcoffset() is None:
        raise ExoError(
            400,
            f"{field_name} must include a timezone offset (e.g. '+10:00' or 'Z'); "
            f"got '{value}'",
            code="invalid_argument",
            retryable=False,
        )
    return parsed.astimezone(UTC)


async def _read_auto_reply(client: ExoClient, identity: str) -> dict | None:
    records = await client.invoke("Get-MailboxAutoReplyConfiguration", {"Identity": identity})
    if not records:
        return None
    projected = project(records[0], _AUTO_REPLY_FIELDS)
    for key in ("start_time", "end_time"):
        if key in projected:
            projected[key] = _normalize_upstream_time(projected[key])
    return projected


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

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def exo_set_mailbox_forwarding(
        identity: Annotated[
            str,
            Field(description="Mailbox whose mail is forwarded. UPN, primary SMTP "
                              "address, alias or GUID."),
        ],
        forward_to: Annotated[
            str | None,
            Field(description="Recipient inside this tenant to forward to — a mailbox, "
                              "mail user, distribution group or mail contact, named by "
                              "UPN, SMTP address, alias or GUID. Leave unset to turn "
                              "forwarding off."),
        ] = None,
        deliver_and_forward: Annotated[
            bool,
            Field(description="Keep a copy of each message in the original mailbox as "
                              "well as forwarding it. False forwards only, leaving the "
                              "original mailbox empty of new mail."),
        ] = True,
    ) -> str:
        """Forward a mailbox's incoming mail to someone else in the same tenant.

        For a departure or an absence, when mail must keep reaching a
        colleague. The target must be a recipient inside this tenant;
        outside addresses are not supported.

        The mailbox keeps its own copy unless deliver_and_forward is false.
        Call without forward_to to switch forwarding off — that also clears
        any external forwarding set elsewhere. Reports what it replaced.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            before = await _read_mailbox(client, identity)
        except ExoError as exc:
            return exc.to_envelope()
        if before is None:
            return ExoError(404, f"No mailbox found for '{identity}'").to_envelope()

        if forward_to:
            parameters = {
                "Identity": identity,
                "ForwardingAddress": forward_to,
                # An address left in the SMTP field would keep forwarding to
                # that target alongside the new one.
                "ForwardingSmtpAddress": CLEAR,
                "DeliverToMailboxAndForward": deliver_and_forward,
            }
        else:
            parameters = {
                "Identity": identity,
                "ForwardingAddress": CLEAR,
                "ForwardingSmtpAddress": CLEAR,
                # Left set, it would silently apply to whoever configures
                # forwarding next.
                "DeliverToMailboxAndForward": False,
            }

        try:
            await client.invoke("Set-Mailbox", parameters)
        except ExoError as exc:
            # The mailbox resolved a moment ago, so an unresolved recipient now
            # is the forwarding target — usually an address outside the tenant,
            # which ForwardingAddress cannot take. Passed through unchanged it
            # reads as "that mailbox does not exist".
            if forward_to and exc.code == "not_found":
                return error_envelope(
                    "invalid_argument",
                    f"Cannot forward to '{forward_to}': it is not a recipient in this "
                    "tenant. Forwarding targets must be a mailbox, mail user, "
                    "distribution group or mail contact inside the tenant — external "
                    "addresses are not supported.",
                    False,
                )
            return exc.to_envelope()

        result: dict = {"identity": identity, "previous_forwarding": _forwarding_state(before)}
        try:
            after = await _read_mailbox(client, identity)
        except ExoError:
            after = None
        if after is None:
            # The write itself succeeded; only the read-back failed.
            result["verified"] = False
            result["forwarding_enabled"] = bool(forward_to)
        else:
            state = _forwarding_state(after)
            result["forwarding"] = state
            result["forwarding_enabled"] = bool(
                state.get("forwarding_address") or state.get("forwarding_smtp_address")
            )
        return dump_json_capped(result)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_get_auto_reply(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
    ) -> str:
        """Read a mailbox's automatic-reply (out-of-office) configuration.

        Read-only — no write. Returns state, external audience, both message
        bodies, and the scheduled start/end. start_time/end_time are always
        reported as UTC with a trailing Z, regardless of how they were set.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            config = await _read_auto_reply(client, identity)
        except ExoError as exc:
            return exc.to_envelope()
        if config is None:
            return ExoError(
                404, f"No mailbox found for '{identity}'", code="not_found"
            ).to_envelope()

        return dump_json_capped({"identity": identity, **config})

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def exo_set_auto_reply(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        auto_reply_state: Annotated[
            str | None,
            Field(
                description=(
                    "One of Disabled, Enabled, Scheduled. Leave unset to keep the "
                    "current value."
                )
            ),
        ] = None,
        external_audience: Annotated[
            str | None,
            Field(
                description=(
                    "One of None, Known, All. Leave unset to keep the current value."
                )
            ),
        ] = None,
        internal_message: Annotated[
            str | None,
            Field(
                description=(
                    "Message shown to senders inside the tenant. Pass an empty "
                    "string to clear it; leave unset to keep the current value."
                )
            ),
        ] = None,
        external_message: Annotated[
            str | None,
            Field(
                description=(
                    "Message shown to senders outside the tenant. Pass an empty "
                    "string to clear it; leave unset to keep the current value."
                )
            ),
        ] = None,
        start_time: Annotated[
            str | None,
            Field(
                description=(
                    "ISO-8601 date/time with a required timezone offset, e.g. "
                    "'2026-01-10T09:00:00+10:00'. Converted to UTC before writing. "
                    "Leave unset to keep the current value."
                )
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(
                description=(
                    "ISO-8601 date/time with a required timezone offset. Converted "
                    "to UTC before writing. Leave unset to keep the current value."
                )
            ),
        ] = None,
    ) -> str:
        """Update a mailbox's automatic-reply configuration.

        Only the fields you supply are changed; the rest keep their current
        value. start_time/end_time must carry a timezone offset (rejected if
        not) and are converted to UTC before writing. Reads the config back
        once after writing and returns the persisted result, not an echo.
        """
        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        if auto_reply_state is not None and auto_reply_state not in _AUTO_REPLY_STATES:
            return ExoError(
                400,
                f"auto_reply_state must be one of {sorted(_AUTO_REPLY_STATES)}, got "
                f"'{auto_reply_state}'",
                code="invalid_argument",
                retryable=False,
            ).to_envelope()
        if external_audience is not None and external_audience not in _EXTERNAL_AUDIENCES:
            return ExoError(
                400,
                f"external_audience must be one of {sorted(_EXTERNAL_AUDIENCES)}, got "
                f"'{external_audience}'",
                code="invalid_argument",
                retryable=False,
            ).to_envelope()

        try:
            parsed_start = (
                _parse_offset_datetime(start_time, "start_time")
                if start_time is not None
                else None
            )
            parsed_end = (
                _parse_offset_datetime(end_time, "end_time") if end_time is not None else None
            )
        except ExoError as exc:
            return exc.to_envelope()

        parameters = {
            "Identity": identity,
            "AutoReplyState": auto_reply_state,
            "ExternalAudience": external_audience,
            "InternalMessage": internal_message,
            "ExternalMessage": external_message,
            "StartTime": _datetime_to_z(parsed_start) if parsed_start is not None else None,
            "EndTime": _datetime_to_z(parsed_end) if parsed_end is not None else None,
        }
        try:
            await client.invoke("Set-MailboxAutoReplyConfiguration", parameters)
        except ExoError as exc:
            return exc.to_envelope()

        try:
            config = await _read_auto_reply(client, identity)
        except ExoError:
            config = None
        if config is None:
            # The write itself succeeded; only the read-back failed.
            return dump_json_capped(
                {"identity": identity, "write_succeeded": True, "verified": False}
            )
        return dump_json_capped({"identity": identity, **config})
