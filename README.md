# exchange-online-mcp

Exchange Online MCP server — exposes the mailbox, distribution-group and mobile-device
operations that **Microsoft Graph cannot express**, as MCP tools over Streamable HTTP.

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## What this is for

Microsoft 365 user offboarding needs a handful of actions that only Exchange owns:

| Action | Cmdlet behind it | Why Graph cannot do it |
|---|---|---|
| Read a mailbox's type, size, archive and hold state | `Get-Mailbox` + `Get-MailboxStatistics` | Graph has no *mailbox* management object; usage is only available as a tenant-level report with up to 48h latency, unusable as a pre-action gate |
| Convert a user mailbox to a shared mailbox | `Set-Mailbox -Type Shared` | The Entra user object has no concept of mailbox type |
| Hide a mailbox from the global address list | `Set-Mailbox -HiddenFromAddressListsEnabled` | Graph's `showInAddressList` is not authoritative for mailbox-enabled users |
| Remove a member from a distribution / mail-enabled security group | `Remove-DistributionGroupMember` | Graph rejects membership writes on these Exchange-owned objects |
| List / remove a mailbox's mobile devices | `Get-MobileDevice` / `Remove-MobileDevice` | Graph exposes Intune `managedDevices` only, never Exchange ActiveSync device partnerships |

Use this server **alongside** a Microsoft Graph MCP server: user accounts, sign-in
blocking, licences, Microsoft 365 groups and security groups stay on the Graph side.

No PowerShell runtime is involved. Every cmdlet runs through the same REST entry point
the Exchange Online PowerShell module itself uses:

```
POST https://outlook.office365.com/adminapi/beta/{tenant}/InvokeCommand
{"CmdletInput": {"CmdletName": "Get-Mailbox", "Parameters": {"Identity": "alice@contoso.com"}}}
```

## Tools

| Tool | Semantics |
|---|---|
| `exo_get_mailbox(identity, include_statistics=True)` | read-only. Returns the mailbox, its statistics, and a `shared_conversion` block (`size_gb`, `over_limit`, `archive_enabled`, `litigation_hold_enabled`, `license_required`) answering "can this become a licence-free shared mailbox?" |
| `exo_convert_mailbox_to_shared(identity)` | write, destructive. `Set-Mailbox -Type Shared`, then reads the mailbox back to confirm |
| `exo_set_mailbox_hidden(identity, hidden)` | write, idempotent, reversible |
| `exo_set_mailbox_forwarding(identity, forward_to=None, deliver_and_forward=True)` | write, idempotent, reversible. Forwards to a recipient **inside the tenant** only (`ForwardingAddress`); omit `forward_to` to switch forwarding off, which also clears any external `ForwardingSmtpAddress` set elsewhere. Reports `previous_forwarding` |
| `exo_add_distribution_group_member(group, member, bypass_security_group_manager_check=False)` | write, idempotent. Repeating an add for an existing member reports `already_member` instead of failing. Rejects dynamic distribution groups, on-premises-synchronized groups and Microsoft 365 Groups with a distinct named error each — none of those can be managed through this tool |
| `exo_remove_distribution_group_member(group, member, bypass_security_group_manager_check=False)` | write, destructive. One named member per call — no bulk removal |
| `exo_get_auto_reply(identity)` | read-only. Returns the mailbox's automatic-reply state, audience, both message bodies, and the scheduled window. `start_time`/`end_time` are always UTC with a trailing `Z` |
| `exo_set_auto_reply(identity, auto_reply_state=None, external_audience=None, internal_message=None, external_message=None, start_time=None, end_time=None)` | write, idempotent. Only supplied fields change — unsupplied fields keep their current value. `start_time`/`end_time` require a timezone offset and are converted to UTC before writing; reads the configuration back once after writing and returns the persisted result |
| `exo_list_mobile_devices(mailbox, limit=50)` | read-only. `limit` 1–200, results truncated with `has_more` |
| `exo_remove_mobile_device(device_id)` | write, destructive. Removes the mailbox partnership only — it does **not** wipe data on the handset |
| `exo_add_mailbox_permission(identity, user, auto_mapping=True)` | write. Grants the trustee FullAccess (fixed, not caller-settable). Errors if already granted — check with `exo_get_mailbox_permission` first |
| `exo_remove_mailbox_permission(identity, user)` | write, destructive. Revokes the trustee's FullAccess (fixed). Errors if not currently granted |
| `exo_add_recipient_permission(identity, trustee)` | write. Grants the trustee SendAs (fixed, not caller-settable). Errors if already granted — check with `exo_get_recipient_permission` first |
| `exo_remove_recipient_permission(identity, trustee)` | write, destructive. Revokes the trustee's SendAs (fixed). Errors if not currently granted |
| `exo_get_mailbox_permission(identity, user=None)` | read-only. Lists trustees holding FullAccess, or narrows to one trustee if `user` is given. Only `FullAccess` grants are reported; the mailbox owner's own implicit self-grant is excluded when listing everyone. Empty list, not an error, when none match |
| `exo_get_recipient_permission(identity, trustee=None)` | read-only. Lists trustees holding SendAs, or narrows to one trustee if `trustee` is given. Only `SendAs` grants are reported; the mailbox owner's own implicit self-grant is excluded when listing everyone. Empty list, not an error, when none match |

Cmdlet output is projected onto a field whitelist and the `@odata.type` companion keys
are stripped, so a typical response is under 1.5 KB instead of the ~40 KB a raw
`Get-Mailbox` returns.

## Credentials (HTTP headers)

The app-only credential is either a **certificate** or a **client secret** — send one,
not both — and this server mints the access token from it per request. Credentials are
read from headers only; there is no environment-variable fallback, and nothing is cached
between requests.

The Exchange Online PowerShell module accepts only certificates for app-only sign-in,
which is why Exchange app-only access is often described as certificate-only. That is a
module limitation: the admin REST endpoint used here takes an ordinary
`client_credentials` token, and the [Admin API authentication
reference](https://learn.microsoft.com/en-us/exchange/reference/admin-api-authentication)
documents `client_secret` alongside the certificate assertion. The secret form has not
yet been confirmed against the undocumented `adminapi/beta` InvokeCommand path this
server calls, so the certificate form remains the proven one.

| Header | Required | Meaning | Where it comes from |
|---|---|---|---|
| `X-Exo-Tenant-Id` | Yes | Tenant GUID **or** default domain (`contoso.onmicrosoft.com`). A domain also lets the server anchor app-only calls on the tenant system mailbox, which some cmdlets need | Entra admin center → Overview |
| `X-Exo-Client-Id` | Yes | Application (client) ID of the registered app | Entra admin center → App registrations → your app → Overview |
| `X-Exo-Certificate` | One of the two | Base64 of the certificate **and** private key: either a PEM bundle or a PKCS#12 (`.pfx`) blob | Generated when you create the app credential (see below) |
| `X-Exo-Client-Secret` | One of the two | Client secret value of the registered app | Entra admin center → your app → Certificates & secrets → Client secrets |
| `X-Exo-Certificate-Password` | No | Password for the `.pfx` / encrypted PEM. Omit when there is none | — |
| `X-Exo-Anchor-Mailbox` | No | Overrides the `X-AnchorMailbox` routing hint, e.g. `UPN:admin@contoso.com` | — |

Missing the tenant or application id — or both credential headers at once — on `/mcp`
returns **401** with the missing names listed. When a certificate and a secret both
arrive, the certificate wins.

### Tenant setup

1. **Entra admin center → App registrations → New registration** (single tenant).
2. **API permissions → Add a permission → APIs my organization uses → Office 365
   Exchange Online → Application permissions → `Exchange.ManageAsApp`** → *Grant admin
   consent*.
3. **Create the app credential.** Either a client secret (*Certificates & secrets →
   Client secrets → New client secret* → copy the value into `X-Exo-Client-Secret`), or a
   certificate whose public half is uploaded to the app (*Certificates & secrets →
   Certificates → Upload certificate*):
   ```bash
   openssl req -x509 -newkey rsa:2048 -sha256 -days 730 -nodes \
     -keyout exo.key -out exo.crt -subj "/CN=mspbots-exo-mcp"
   openssl pkcs12 -export -out exo.pfx -inkey exo.key -in exo.crt   # upload exo.crt
   base64 -w0 exo.pfx                                               # X-Exo-Certificate
   ```
   A PEM bundle works just as well: `base64 -w0 <(cat exo.key exo.crt)`.
4. **Assign an Exchange role to the service principal** — Entra admin center → Roles and
   administrators, or Exchange admin center → Roles → Admin roles. `Recipient Management`
   covers every tool here; `View-Only Organization Management` is enough for the two
   read-only tools. Without a role assignment every cmdlet returns `unauthorized`.

Both credential forms expire: rotate the certificate before `notAfter` and re-upload it,
or renew the secret before its expiry. One credential per tenant — this server never
shares credentials across tenants.

## Endpoints

| Endpoint | Behaviour |
|---|---|
| `POST /mcp` | MCP Streamable HTTP. Requires the credential headers |
| `GET /health` | `{"status": "ok"}`. Purely local probe — never touches Exchange Online |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MCP_HTTP_HOST` | `0.0.0.0` | Listening host |
| `MCP_HTTP_PORT` | `8080` | Listening port |
| `EXO_BASE_URL` | `https://outlook.office365.com` | Admin endpoint host; override for sovereign clouds (GCC High, DoD, 21Vianet) |
| `ENTRA_LOGIN_BASE_URL` | `https://login.microsoftonline.com` | Entra login host; same reason |

Unknown environment variables are ignored, never fatal. No credential ever comes from
the environment.

## Errors

Tools never raise; they return a JSON envelope as their string result:

```json
{"error": {"code": "unauthorized", "message": "...", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`, `dynamic_group_unsupported`,
`directory_synced_group_unsupported`, `microsoft_365_group_unsupported`. The last three
are returned only by `exo_add_distribution_group_member`, for the three group types it
cannot manage. Exchange reports a missing recipient or device as a
`400` carrying a cmdlet exception; those are normalised to `not_found`. An empty list is
a successful empty result, never `not_found`.

One upstream quirk shapes the transport: Exchange denies an unroled app with a `403` whose
body is `Content-Length` bytes of NUL, labelled `Content-Encoding: gzip`. Left alone that
makes httpx raise `DecodingError` while reading the body — a subclass of `RequestError` —
so the status would be lost and a deterministic `403` retried as a network fault. Admin
calls therefore send `Accept-Encoding: identity`, statuses with no usable body get a
message derived from the status, and an unreadable body is never reported as an empty
result.

## Run it

```bash
docker compose up --build          # or: docker build --platform linux/amd64 -t exchange-online-mcp:dev .
curl http://localhost:8080/health
```

```bash
uv sync
uv run python -m exo_mcp
```

## Tests

```bash
uv run pytest                                   # 84 unit tests, no network
docker build --platform linux/amd64 -t exchange-online-mcp:dev .
uv run python tests/mock_e2e/run_e2e.py         # real container, mocked Entra + Exchange
```

`tests/mock_e2e/` runs the built image against a local stand-in for Entra ID and the
admin endpoint: it generates a throwaway certificate, verifies the client assertion the
container signs (RS256, `x5t`, audience), and drives
`401 → initialize → tools/list → tools/call` for all ten tools. No customer tenant
required.

## Delivery checklist (SOP §14 self-assessment)

Integration contract
- [x] `POST /mcp` Streamable HTTP; `GET /health` returns 200 `{"status":"ok"}`, local-only probe
- [x] Defaults to `0.0.0.0:8080`
- [x] Unknown environment variables ignored (`SettingsConfigDict(extra="ignore")`)
- [x] No session stickiness, no local persistent state
- [x] Credentials read from headers only — no env fallback, no credential fields in config
- [x] Header names follow `X-<Vendor>-<Credential>` and match this README character for character
- [x] Missing headers → 401 listing them
- [x] Request-scoped isolation via `contextvars`, reset in `finally`; no global credential state
- [x] No cross-request caching of tenant data or derived tokens (one token exchange per tool call)
- [x] DNS-rebinding protection disabled
- [x] MCP app's lifespan mounted on the outer Starlette app
- [x] `tools/call` verified over real HTTP with headers (`tests/mock_e2e/`)

Errors and network
- [x] Structured error envelope, fixed code vocabulary, no exceptions raised
- [x] Empty results are empty, not `not_found`; messages carry no credentials
- [x] Outbound timeouts (connect 5s / read 30s; token exchange read 15s)
- [x] Limited retry with backoff on 429/5xx honouring `Retry-After`; worst case well under 120s
- [x] One shared connection pool, reused within a request

Container and deliverables
- [x] `docker build --platform linux/amd64` builds and runs
- [x] Multi-stage build, production stage runs as non-root `app`
- [x] `curl` installed in the production stage
- [x] `EXPOSE 8080`, default `MCP_HTTP_PORT`/`MCP_HTTP_HOST`, `HEALTHCHECK` configured
- [x] `uv.lock` committed, no private dependency sources
- [x] Credential header table present (above)

Agent-facing design
- [x] 10 tools, modelled on the offboarding flow rather than on endpoints
- [x] Every description ≤ 500 chars (longest 404), first line ≤ 100 chars
- [x] Service-level `instructions` provided (1027 chars)
- [x] Tools self-describing; required/optional parameters explicit
- [x] All tools prefixed `exo_`
- [x] `exo_list_mobile_devices` has `limit` (default 50, hard cap 200) with `has_more`
- [x] Returns capped at 20,000 chars, compact separators, `ensure_ascii=False`
- [x] `readOnlyHint` / `destructiveHint` / `idempotentHint` set on every tool
- [x] Write tools justified by the offboarding SOP; single named resource only, no bulk deletes
- [ ] Business flow run by an agent against a real tenant — pending a customer sandbox

Logging and sensitive information
- [x] No credentials, tokens or certificate material logged or echoed in error messages
- [x] No real credentials anywhere in the repo; certificates are generated per test run
- [x] `.env` / `.venv` / `*.pem` / `*.pfx` / `*.key` excluded in `.gitignore` and `.dockerignore`

Known follow-ups for the first real tenant
- Whether `X-AnchorMailbox` is required for every cmdlet under app-only auth, and whether
  the tenant-GUID form (which yields no anchor) is sufficient.
- `BypassSecurityGroupManagerCheck` is now opt-in (PRD-20394 INT finding, 2026-10-14): with
  the app holding only Exchange Recipient Administrator, any call that carried the switch —
  even set to false — was refused with 403. The switch is sent only when the caller passes
  `bypass_security_group_manager_check=true`; a denial then reports `bypass_not_permitted`,
  and Exchange's owner check reports `not_group_manager`. Still to confirm on a real tenant:
  whether Recipient Management can change a group's membership without the switch when the
  app is not a group owner.
