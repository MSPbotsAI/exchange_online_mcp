# Module: `src/exo_mcp/tools/permissions.py` (mailbox/recipient permission grants)

First entry in this module doc (no prior accumulated facts existed in this repo for this file).

## PRD-20480 · 2026-10-14

- New tool `exo_add_sendonbehalf_permission` (`src/exo_mcp/tools/permissions.py` @ `3ec7afb`), placed right after `exo_add_mailbox_permission`. Additive `Set-Mailbox -GrantSendOnBehalfTo @{Add=[delegate]}` — a different right and a different cmdlet from `exo_add_mailbox_permission`'s `Add-MailboxPermission`/FullAccess: SendOnBehalf shows mail as sent "on behalf of" the mailbox rather than granting full access to it. Existing delegates are left in place (additive, not a replace). Same `verified: false` + replication-delay note pattern as the `Set-Mailbox`-based tools in `mailbox.py`.
- This is this module's first tool to use a multi-valued `{"Add": [...]}` cmdlet parameter — every prior tool in this file (`exo_add_mailbox_permission`, `exo_add_recipient_permission`, and their `Remove-*` counterparts) sets a fixed scalar `AccessRights` value. Not yet exercised against the real EXO REST endpoint (same open question as `exo_add_email_alias` in `mailbox.py`, tracked as TC-EXO-004); the documented fallback if rejected is read-merge-write via `exo_get_mailbox` + full-list `Set-Mailbox`.
- No new Exchange role needed — `Recipient Management` (this repo's existing single required role, per README) already covers `Set-Mailbox`, so this tool has no cross-repo scope-grant dependency, unlike `ms-graph-mcp`'s `graph_create_tap`/`graph_add_group_owner` half of the same ticket.
- Test coverage: `tests/test_tool_calls.py::test_add_sendonbehalf_permission_grants_delegate`, `::test_add_sendonbehalf_permission_not_found` (both added in this ticket, both passing at `3ec7afb`).
- Not covered by automated tests (needs a real tenant — see `docs/tickets/PRD-20480.md` § Self-test, TC-EXO-001b): whether a real EXO tenant's `GrantSendOnBehalfTo` actually no-ops a duplicate `Add` (additive-idempotent assumption, not yet confirmed) and whether `exo_get_mailbox` shows the delegate after replication.
