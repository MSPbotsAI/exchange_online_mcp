# exchange-online-mcp

Exchange Online MCP server — exposes the mailbox, distribution-group and mobile-device
operations that **Microsoft Graph cannot express**, as MCP tools over Streamable HTTP.

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
| `exo_remove_distribution_group_member(group, member, bypass_security_group_manager_check=True)` | write, destructive. One named member per call — no bulk removal |
| `exo_list_mobile_devices(mailbox, limit=50)` | read-only. `limit` 1–200, results truncated with `has_more` |
| `exo_remove_mobile_device(device_id)` | write, destructive. Removes the mailbox partnership only — it does **not** wipe data on the handset |

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
`rate_limited`, `upstream_error`. Exchange reports a missing recipient or device as a
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
uv run pytest                                   # 50 unit tests, no network
docker build --platform linux/amd64 -t exchange-online-mcp:dev .
uv run python tests/mock_e2e/run_e2e.py         # real container, mocked Entra + Exchange
```

`tests/mock_e2e/` runs the built image against a local stand-in for Entra ID and the
admin endpoint: it generates a throwaway certificate, verifies the client assertion the
container signs (RS256, `x5t`, audience), and drives
`401 → initialize → tools/list → tools/call` for all six tools. No customer tenant
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
- [x] 6 tools, modelled on the offboarding flow rather than on endpoints
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
- Whether `BypassSecurityGroupManagerCheck` is needed for mail-enabled security groups in
  practice, or should become opt-in.
