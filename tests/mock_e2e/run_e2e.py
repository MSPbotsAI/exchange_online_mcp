"""End-to-end check of the built container image, no customer tenant needed.

Starts mock_upstream.py in-process, runs the image against it with a throwaway
certificate, and drives the real MCP protocol over HTTP:
missing-header 401 -> initialize -> tools/list -> tools/call for every tool.

    docker build --platform linux/amd64 -t exchange-online-mcp:dev .
    uv run python tests/mock_e2e/run_e2e.py

Exits non-zero on the first failed assertion. Nothing here touches a real
tenant, and the certificate is generated per run.
"""

import base64
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from mock_upstream import serve  # noqa: E402

IMAGE = "exchange-online-mcp:dev"
CONTAINER = "exchange-online-mcp-e2e"
MCP_PORT = 18080
MOCK_PORT = 18765
TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def make_certificate() -> tuple[str, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "exo-mcp-e2e")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    bundle = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + cert.public_bytes(serialization.Encoding.PEM)
    return base64.b64encode(bundle).decode(), cert


def post(url: str, payload: dict, headers: dict) -> tuple[int, str, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def rpc_result(raw: str) -> dict:
    """Pull the JSON-RPC payload out of a JSON or SSE response body."""
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(raw)


def main() -> int:
    certificate_b64, certificate = make_certificate()
    creds = {
        "X-Exo-Tenant-Id": TENANT,
        "X-Exo-Client-Id": CLIENT_ID,
        "X-Exo-Certificate": certificate_b64,
    }
    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **creds,
    }

    server = serve(MOCK_PORT, certificate)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER, "--network", "host",
            "-e", f"MCP_HTTP_PORT={MCP_PORT}",
            "-e", f"EXO_BASE_URL=http://127.0.0.1:{MOCK_PORT}",
            "-e", f"ENTRA_LOGIN_BASE_URL=http://127.0.0.1:{MOCK_PORT}",
            # The runtime injects variables this project never declares; the
            # container must tolerate them (SOP 1.2).
            "-e", "UNDECLARED_RUNTIME_VARIABLE=surprise",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )

    try:
        print("health")
        healthy = False
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{MCP_PORT}/health", timeout=2
                ) as resp:
                    healthy = resp.status == 200 and json.loads(resp.read())["status"] == "ok"
                    break
            except Exception:
                time.sleep(0.5)
        check("GET /health returns {\"status\":\"ok\"}", healthy)
        if not healthy:
            logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True)
            print(logs.stderr.decode())
            return 1

        print("credential gate")
        status, body, _ = post(
            MCP_URL,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        payload = json.loads(body)
        check("POST /mcp without credentials returns 401", status == 401)
        check(
            "401 body lists the required headers",
            payload.get("required_headers")
            == [
                "X-Exo-Tenant-Id",
                "X-Exo-Client-Id",
                "X-Exo-Certificate or X-Exo-Client-Secret",
            ],
            json.dumps(payload.get("required_headers")),
        )

        print("session")
        status, body, headers = post(
            MCP_URL,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "0"},
                },
            },
            mcp_headers,
        )
        session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        initialize = rpc_result(body)
        check("initialize succeeds", status == 200 and "result" in initialize)
        instructions = initialize.get("result", {}).get("instructions", "")
        check("service instructions are present and <= 1500 chars", 0 < len(instructions) <= 1500,
              f"{len(instructions)} chars")
        session_headers = {**mcp_headers, "Mcp-Session-Id": session_id or ""}
        post(
            MCP_URL,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_headers,
        )

        print("tools/list")
        status, body, _ = post(
            MCP_URL, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_headers
        )
        tools = rpc_result(body)["result"]["tools"]
        names = sorted(tool["name"] for tool in tools)
        check(
            "6 exo_ tools listed",
            names
            == [
                "exo_convert_mailbox_to_shared",
                "exo_get_mailbox",
                "exo_list_mobile_devices",
                "exo_remove_distribution_group_member",
                "exo_remove_mobile_device",
                "exo_set_mailbox_hidden",
            ],
            ", ".join(names),
        )
        longest = max(len(tool.get("description") or "") for tool in tools)
        check("every description <= 500 chars", longest <= 500, f"longest {longest}")
        print(f"        tools/list payload: {len(body)} chars")

        print("tools/call")
        calls = [
            ("exo_get_mailbox", {"identity": "alice@contoso.com"}),
            ("exo_convert_mailbox_to_shared", {"identity": "alice@contoso.com"}),
            ("exo_set_mailbox_hidden", {"identity": "alice@contoso.com", "hidden": True}),
            (
                "exo_remove_distribution_group_member",
                {"group": "all-staff@contoso.com", "member": "alice@contoso.com"},
            ),
            ("exo_list_mobile_devices", {"mailbox": "alice@contoso.com"}),
            (
                "exo_remove_mobile_device",
                {"device_id": "11111111-0000-0000-0000-000000000000"},
            ),
        ]
        for index, (name, arguments) in enumerate(calls, start=3):
            status, body, _ = post(
                MCP_URL,
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                session_headers,
            )
            result = rpc_result(body).get("result", {})
            text = (result.get("content") or [{}])[0].get("text", "")
            parsed = json.loads(text) if text else {}
            check(
                f"{name} returns a non-error result",
                status == 200 and "error" not in parsed,
                text[:160],
            )
            check(f"{name} return body <= 20000 chars", len(text) <= 20000, f"{len(text)} chars")

        print("upstream expectations")
        from mock_upstream import Handler

        cmdlets = [call["cmdlet"] for call in Handler.calls if "cmdlet" in call]
        tokens = [call for call in Handler.calls if call.get("token")]
        check("every assertion was accepted", all(c["token"] == "issued" for c in tokens),
              f"{len(tokens)} token requests")
        check(
            "cmdlets invoked in order",
            cmdlets
            == [
                "Get-Mailbox",
                "Get-MailboxStatistics",
                "Set-Mailbox",
                "Get-Mailbox",
                "Set-Mailbox",
                "Get-Mailbox",
                "Remove-DistributionGroupMember",
                "Get-MobileDevice",
                "Remove-MobileDevice",
            ],
            ", ".join(cmdlets),
        )
        anchors = {call["anchor"] for call in Handler.calls if "cmdlet" in call}
        check(
            "app-only calls are anchored on the tenant system mailbox",
            anchors
            == {f"UPN:SystemMailbox{{bb558c35-97f1-4cb9-8ff7-d53741dc928c}}@{TENANT}"},
            str(anchors),
        )
        check(
            "one token exchange per tool call, nothing cached across requests",
            len(tokens) == len(calls),
            f"{len(tokens)} exchanges for {len(calls)} tool calls",
        )
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        server.shutdown()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All e2e checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
