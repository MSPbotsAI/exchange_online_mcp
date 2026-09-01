"""tools/list snapshot, agent-facing description budget, and error mapping.

No network: tool enumeration goes through FastMCP's in-process list_tools(),
and the error-code mapping is checked directly against ExoError.
"""

import json

import pytest

from exo_mcp.api_client import ExoError
from exo_mcp.config import Settings
from exo_mcp.server import create_mcp_server

# Tool name -> required parameters. This snapshot IS the outward contract:
# a red assertion here means downstream agents/tests see a breaking change.
EXPECTED_TOOLS = {
    "exo_get_mailbox": {"identity"},
    "exo_convert_mailbox_to_shared": {"identity"},
    "exo_set_mailbox_hidden": {"identity", "hidden"},
    "exo_remove_distribution_group_member": {"group", "member"},
    "exo_list_mobile_devices": {"mailbox"},
    "exo_remove_mobile_device": {"device_id"},
}

_READ_ONLY = {"exo_get_mailbox", "exo_list_mobile_devices"}
_DESTRUCTIVE = {
    "exo_convert_mailbox_to_shared",
    "exo_remove_distribution_group_member",
    "exo_remove_mobile_device",
}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"
    assert len(names) <= 20, "tool count should stay within the SOP's <=20 guideline"

    by_name = {tool.name: tool for tool in tools}
    for name, expected_required in EXPECTED_TOOLS.items():
        tool = by_name[name]
        assert set(tool.inputSchema.get("required", [])) == expected_required, name
        assert name.startswith("exo_"), f"{name}: missing vendor prefix"

        description = tool.description or ""
        assert len(description) <= 500, f"{name}: description too long ({len(description)})"
        first_line = description.strip().splitlines()[0]
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"
        assert "API:" not in description, f"{name}: leaked implementation detail"

        assert tool.annotations is not None, name
        if name in _READ_ONLY:
            assert tool.annotations.readOnlyHint is True, name
        else:
            assert not tool.annotations.readOnlyHint, name
        if name in _DESTRUCTIVE:
            assert tool.annotations.destructiveHint is True, name


@pytest.mark.asyncio
async def test_service_instructions_present_and_bounded():
    mcp = create_mcp_server(Settings())
    assert mcp.instructions
    assert len(mcp.instructions) <= 1500


@pytest.mark.asyncio
async def test_tools_without_credentials_return_not_configured():
    """Outside a request context there are no credentials, and tools must
    return the error envelope rather than raise."""
    mcp = create_mcp_server(Settings())
    for name, required in EXPECTED_TOOLS.items():
        args = {key: (True if key == "hidden" else "x") for key in required}
        result = await mcp.call_tool(name, args)
        # FastMCP returns (content, structured) or just content depending on version.
        content = result[0] if isinstance(result, tuple) else result
        payload = json.loads(content[0].text)
        assert payload["error"]["code"] == "not_configured", name


@pytest.mark.parametrize(
    "status_code,message,expected_code,expected_retryable",
    [
        (0, "network down", "upstream_error", True),
        (400, "bad parameter", "invalid_argument", False),
        (400, "ManagementObjectNotFoundException: nope", "not_found", False),
        (
            400,
            "The operation couldn't be performed because object couldn't be found",
            "not_found",
            False,
        ),
        (401, "cert rejected", "unauthorized", False),
        (403, "no role assignment", "unauthorized", False),
        (404, "missing", "not_found", False),
        (422, "unprocessable", "invalid_argument", False),
        (429, "throttled", "rate_limited", True),
        (500, "boom", "upstream_error", True),
        (503, "unavailable", "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, message, expected_code, expected_retryable):
    envelope = json.loads(ExoError(status_code, message).to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert envelope["error"]["message"] == message
