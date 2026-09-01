"""Stand-in for Entra ID and the Exchange Online admin endpoint.

Lets the real container be exercised end to end without a customer tenant: it
verifies the client assertion the container signs (thumbprint, audience,
signature), then answers cmdlets from fixtures.
"""

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import hashes

ACCESS_TOKEN = "mock-exo-access-token"

# cmdlet -> records the mock returns
FIXTURES: dict[str, list[dict]] = {
    "Get-Mailbox": [
        {
            "Identity": "contoso.onmicrosoft.com/Users/alice",
            "Identity@odata.type": "#String",
            "UserPrincipalName": "alice@contoso.com",
            "PrimarySmtpAddress": "alice@contoso.com",
            "DisplayName": "Alice Smith",
            "RecipientTypeDetails": "UserMailbox",
            "HiddenFromAddressListsEnabled": False,
            "LitigationHoldEnabled": False,
            "InPlaceHolds": [],
            "ArchiveStatus": "None",
            "AccountDisabled": True,
            "ProhibitSendReceiveQuota": "50 GB (53,687,091,200 bytes)",
            "WhenMailboxCreated": "2019-04-02T13:22:11",
            # Properties a real tenant returns and the tools must drop
            "ThrottlingPolicy": "",
            "ExtensionCustomAttribute1": [],
            "MessageTrackingReadStatusEnabled": True,
        }
    ],
    "Get-MailboxStatistics": [
        {
            "TotalItemSize": "1.66 GB (1,782,579,200 bytes)",
            "ItemCount": 12043,
            "TotalDeletedItemSize": "120.5 MB (126,353,408 bytes)",
            "LastLogonTime": "2026-08-20T08:15:00",
        }
    ],
    "Get-MobileDevice": [
        {
            "Guid": f"11111111-0000-0000-0000-00000000000{i}",
            "Identity": f"contoso.onmicrosoft.com/Users/alice/ExchangeActiveSyncDevices/dev{i}",
            "DeviceId": f"APPL{i}XYZ",
            "DeviceType": "iPhone",
            "DeviceModel": "iPhone14,3",
            "DeviceOS": "iOS 18.1",
            "FriendlyName": f"Alice iPhone {i}",
            "ClientType": "EAS",
            "DeviceAccessState": "Allowed",
            "FirstSyncTime": "2025-01-05T10:00:00",
            "UserDisplayName": "Alice Smith",
            "WhenChangedUTC": "2026-08-20T08:15:00",
        }
        for i in range(3)
    ],
    "Set-Mailbox": [],
    "Remove-DistributionGroupMember": [],
    "Remove-MobileDevice": [],
}


class Handler(BaseHTTPRequestHandler):
    certificate = None  # set by serve(): the cert whose key must sign assertions
    calls: list[dict] = []

    def log_message(self, *_args):  # keep the e2e output readable
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path.endswith("/oauth2/v2.0/token"):
            self._token(path, body)
        elif path.endswith("/InvokeCommand"):
            self._invoke(body)
        else:
            self._json(404, {"error": {"message": f"unexpected path {path}"}})

    def _token(self, path: str, body: bytes):
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        assertion = form.get("client_assertion", "")
        expected_x5t = base64.urlsafe_b64encode(
            self.certificate.fingerprint(hashes.SHA1())
        ).decode()
        try:
            header = jwt.get_unverified_header(assertion)
            assert header["alg"] == "RS256", "assertion must be RS256"
            assert header["x5t"] == expected_x5t, "x5t does not match the certificate"
            audience = f"http://{self.headers['Host']}{path}"
            claims = jwt.decode(
                assertion,
                self.certificate.public_key(),
                algorithms=["RS256"],
                audience=audience,
            )
            assert claims["iss"] == form["client_id"], "iss must be the client id"
            assert form["scope"] == "https://outlook.office365.com/.default"
        except Exception as exc:
            self.calls.append({"token": "rejected", "reason": str(exc)})
            self._json(400, {"error": "invalid_client", "error_description": str(exc)})
            return
        self.calls.append({"token": "issued"})
        self._json(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599})

    def _invoke(self, body: bytes):
        if self.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            self._json(401, {"error": {"message": "missing bearer token"}})
            return
        payload = json.loads(body)
        cmdlet = payload["CmdletInput"]["CmdletName"]
        self.calls.append(
            {
                "cmdlet": cmdlet,
                "parameters": payload["CmdletInput"]["Parameters"],
                "anchor": self.headers.get("X-AnchorMailbox"),
                "prefer": self.headers.get("Prefer"),
            }
        )
        if cmdlet not in FIXTURES:
            self._json(
                400,
                {"error": {"message": f"cmdlet {cmdlet} is not mocked", "details": []}},
            )
            return
        self._json(200, {"Value": FIXTURES[cmdlet]})

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int, certificate) -> ThreadingHTTPServer:
    Handler.certificate = certificate
    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server
