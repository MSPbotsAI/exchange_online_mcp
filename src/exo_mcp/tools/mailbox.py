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
    contains_case_insensitive,
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
    # PRD-20480 D3: exo_add_sendonbehalf_permission/exo_add_email_alias both
    # said "confirm with exo_get_mailbox", but this projection didn't surface
    # the two properties they write — there was nothing to confirm with.
    "EmailAddresses": "email_addresses",
    "GrantSendOnBehalfTo": "grant_send_on_behalf_to",
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


# PRD-20321: a GAL-visibility change reaches other EXO servers through
# directory replication, which took over a minute in a real tenant and can run
# to several. A read issued in the same call lands on the server that took the
# write and echoes the new value at once, so it proves nothing; reads issued
# later from elsewhere can still show the old value for minutes. The tool
# therefore reports the write as accepted but unverified and leaves the check to
# a later exo_get_mailbox.
_HIDDEN_PENDING_NOTE = (
    "Exchange Online accepted the change, but it reaches the global address list "
    "through directory replication, which commonly takes several minutes. Until "
    "then exo_get_mailbox may still show the previous value. Re-check with "
    "exo_get_mailbox after about 5 minutes; if it still shows the previous value, "
    "call this tool again."
)


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


# PRD-20480 D4: a plain address or an explicit lowercase "smtp:" prefix both
# add a secondary (alias) address. Any other prefix -- including "SMTP:",
# which EXO reads as "make this the new primary" -- is out of scope for this
# tool (it would silently demote the current primary, contradicting "existing
# primary is left untouched") and must be rejected per-address rather than
# forwarded to Exchange.
def _invalid_alias_prefix(address: str) -> str | None:
    """Reason the address's prefix is unsupported, or None if it is fine."""
    if ":" not in address:
        return None
    prefix, _, rest = address.partition(":")
    if prefix == "smtp" and rest:
        return None
    return (
        f"'{address}' has an unsupported address-type prefix. Only a plain address or "
        "an explicit lowercase 'smtp:' prefix is accepted here; 'SMTP:' (and any other "
        "prefix) would change the mailbox's primary address, which this tool never does."
    )


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
        archive, no hold). Covers user and shared mailboxes. Also returns
        email_addresses (proxy/alias list) and grant_send_on_behalf_to — use
        this to confirm exo_add_email_alias/exo_add_sendonbehalf_permission
        after EXO has replicated the change.
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

        The change takes several minutes to replicate, so the response is
        never verified: confirm it later with exo_get_mailbox.
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

        return dump_json_capped(
            {
                "identity": identity,
                "requested_hidden": hidden,
                "write_accepted": True,
                "verified": False,
                "note": _HIDDEN_PENDING_NOTE,
            }
        )

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

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def exo_add_email_alias(
        identity: Annotated[
            str,
            Field(description="Mailbox UPN, primary SMTP address, alias or GUID."),
        ],
        aliases: Annotated[
            list[str],
            Field(description="One or more alias addresses to add, e.g. "
                              "[\"old-name@contoso.com\"]. Each one must be a plain "
                              "address or carry an explicit lowercase \"smtp:\" "
                              "prefix; any other prefix (including \"SMTP:\", which "
                              "Exchange reads as \"make this the new primary\") is "
                              "rejected for that address, not forwarded."),
        ],
    ) -> str:
        """Add one or more alias (proxy) addresses to a mailbox (PRD-20480).

        Additive: existing addresses are kept (read-merge-write, not a raw
        Add). Every address is checked first — invalid prefix,
        already_present or in_use_by_other are reported per-address rather
        than failing the whole call; whichever addresses were new are
        still written. Write is confirmed with a read-back.
        """
        if not aliases:
            return error_envelope(
                "invalid_argument", "Provide at least one alias address.", False
            )

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

        mailbox_identity = mailbox.get("identity")
        current_addresses = mailbox.get("email_addresses") or []

        results: list[dict] = []
        to_add: list[str] = []
        for raw in aliases:
            prefix_error = _invalid_alias_prefix(raw)
            if prefix_error is not None:
                results.append({"address": raw, "result": "invalid", "message": prefix_error})
                continue

            local = raw.split(":", 1)[1] if ":" in raw else raw
            wire = f"smtp:{local}"
            if contains_case_insensitive(current_addresses, wire) or contains_case_insensitive(
                current_addresses, f"SMTP:{local}"
            ):
                results.append({"address": raw, "result": "already_present"})
                continue

            # D2: check with Exchange whether this address already belongs
            # to someone before writing it, instead of finding out from an
            # undifferentiated "Invalid Operation" on the merged write.
            try:
                recipients = await client.invoke("Get-Recipient", {"Identity": local})
            except ExoError as exc:
                results.append({"address": raw, "result": "invalid", "message": exc.message})
                continue
            if recipients:
                found = recipients[0]
                found_identity = found.get("Identity")
                if found_identity and found_identity != mailbox_identity:
                    found_label = found.get("PrimarySmtpAddress") or found_identity
                    results.append(
                        {
                            "address": raw,
                            "result": "in_use_by_other",
                            "message": (
                                f"'{local}' is already assigned to a different "
                                f"recipient ('{found_label}')."
                            ),
                        }
                    )
                    continue
                # Resolves to this same mailbox but wasn't in email_addresses
                # (e.g. it is the primary) -- nothing to add.
                results.append({"address": raw, "result": "already_present"})
                continue

            results.append({"address": raw, "result": "added"})
            to_add.append(wire)

        if not to_add:
            return dump_json_capped(
                {
                    "identity": identity,
                    "results": results,
                    "accepted": False,
                    "note": "No new address was written -- see results for why each one "
                    "was skipped.",
                }
            )

        merged = [*current_addresses, *to_add]
        try:
            await client.invoke(
                "Set-Mailbox", {"Identity": identity, "EmailAddresses": merged}
            )
        except ExoError as exc:
            return exc.to_envelope()

        verified = False
        try:
            after = await _read_mailbox(client, identity)
        except ExoError:
            after = None
        if after is not None:
            after_addresses = after.get("email_addresses") or []
            verified = all(contains_case_insensitive(after_addresses, w) for w in to_add)

        response: dict = {
            "identity": identity,
            "results": results,
            "accepted": True,
            "verified": verified,
        }
        if not verified:
            response["note"] = (
                "Exchange Online accepted the change, but it replicates across "
                "the directory, which commonly takes a few minutes. Confirm "
                "with exo_get_mailbox afterwards."
            )
        return dump_json_capped(response)
