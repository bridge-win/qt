"""2026-07-28 discovery, per-request transport, schema and isolation contracts."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qt.agent_hub.api import create_app
from qt.agent_hub.config import ROLES, HubConfig, Role
from qt.agent_hub.mcp import LEGACY, MODERN, PREFIX
from qt.agent_hub.runtime import HubRuntime

AUTH = {"Authorization": "Bearer mcp-contract-fixture"}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HubRuntime:
    monkeypatch.setenv("QT_AGENT_HUB_SERVICE_TOKEN", "mcp-contract-fixture")
    monkeypatch.delenv("QT_AGENT_HUB_RESEARCH_URL", raising=False)
    config = HubConfig(
        tenant_id="alpha",
        auto_execute=True,
        roles={role: Role(instruction="Review evidence") for role in ROLES},
    )
    return HubRuntime(config, tmp_path / "state", tmp_path / "files")


@pytest.fixture
def client(runtime: HubRuntime) -> TestClient:
    return TestClient(create_app(runtime.config, runtime.root, runtime.files, runtime=runtime))


def request(method: str, params: dict | None = None, *, version: str = MODERN) -> tuple[dict, dict]:
    params = {
        **(params or {}),
        "_meta": {
            PREFIX + "protocolVersion": version,
            PREFIX + "clientCapabilities": {},
            PREFIX + "clientInfo": {"name": "contract-test", "version": "1.0.0"},
        },
    }
    headers = {
        **AUTH,
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
    }
    if "name" in params:
        headers["Mcp-Name"] = params["name"]
    return {"jsonrpc": "2.0", "id": "request-one", "method": method, "params": params}, headers


def test_discovery_declares_only_implemented_capabilities(client: TestClient) -> None:
    body, headers = request("server/discover")
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [MODERN, LEGACY]
    assert result["capabilities"] == {"tools": {}}
    assert result["cacheScope"] == "private" and result["ttlMs"] == 300000
    assert result["_meta"][PREFIX + "serverInfo"]["name"] == "qt-agent-hub"
    assert "Mcp-Session-Id" not in response.headers


def test_direct_tool_call_needs_no_handshake_and_never_uses_client_info_as_identity(
    client: TestClient, runtime: HubRuntime
) -> None:
    runtime.context.ingest("alpha", "own", "BTC own evidence")
    runtime.context.ingest("bravo", "private", "BTC private secret")
    body, headers = request(
        "tools/call", {"name": "search_evidence", "arguments": {"query": "BTC"}}
    )
    body["params"]["_meta"][PREFIX + "clientInfo"]["name"] = "bravo"
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    values = json.loads(result["content"][0]["text"])
    assert len(values) == 1 and values[0]["tenant_id"] == "alpha"
    assert "private secret" not in response.text


def test_optional_client_info_may_be_omitted_but_not_malformed(client: TestClient) -> None:
    body, headers = request("tools/list")
    body["params"]["_meta"].pop(PREFIX + "clientInfo")
    assert client.post("/mcp", headers=headers, json=body).status_code == 200
    body["params"]["_meta"][PREFIX + "clientInfo"] = {"name": "incomplete"}
    result = client.post("/mcp", headers=headers, json=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == -32602


@pytest.mark.parametrize("missing", ["protocolVersion", "clientCapabilities"])
def test_each_request_requires_its_own_protocol_metadata(client: TestClient, missing: str) -> None:
    discovery, discovery_headers = request("server/discover")
    assert client.post("/mcp", headers=discovery_headers, json=discovery).status_code == 200
    body, headers = request("tools/list")
    body["params"]["_meta"].pop(PREFIX + missing)
    result = client.post("/mcp", headers=headers, json=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "header,value",
    [
        ("MCP-Protocol-Version", None),
        ("MCP-Protocol-Version", LEGACY),
        ("Mcp-Method", None),
        ("Mcp-Method", "tools/list"),
        ("Mcp-Name", None),
        ("Mcp-Name", "submit_research"),
        ("Mcp-Name", "=?base64?%%%?="),
    ],
)
def test_header_mismatch_rejected_before_any_side_effect(
    client: TestClient, runtime: HubRuntime, header: str, value: str | None
) -> None:
    body, headers = request("tools/call", {"name": "read_status", "arguments": {}})
    if value is None:
        headers.pop(header)
    else:
        headers[header] = value
    result = client.post("/mcp", headers=headers, json=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == -32020
    assert runtime.store.list_jobs("alpha") == []


def test_base64_mcp_name_is_decoded_before_comparison(client: TestClient) -> None:
    body, headers = request("tools/call", {"name": "read_status", "arguments": {}})
    headers["Mcp-Name"] = "=?base64?" + base64.b64encode(b"read_status").decode() + "?="
    assert client.post("/mcp", headers=headers, json=body).status_code == 200


def test_unknown_protocol_lists_supported_versions(client: TestClient) -> None:
    body, headers = request("server/discover", version="2099-01-01")
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32022,
        "message": "Unsupported per-request protocol version",
        "data": {"supported": [MODERN, LEGACY], "requested": "2099-01-01"},
    }


@pytest.mark.parametrize(
    "method", ["initialize", "tasks/list", "subscriptions/listen", "prompts/list"]
)
def test_unimplemented_modern_methods_are_404_not_fake_capabilities(
    client: TestClient, method: str
) -> None:
    body, headers = request(method)
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 404 and response.json()["error"]["code"] == -32601


def test_tool_list_has_valid_fixed_schemas_and_private_cache_scope(client: TestClient) -> None:
    body, headers = request("tools/list")
    result = client.post("/mcp", headers=headers, json=body).json()["result"]
    assert result["cacheScope"] == "private"
    assert [tool["name"] for tool in result["tools"]] == [
        "search_evidence",
        "read_status",
        "submit_research",
    ]
    for tool in result["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
    assert result["tools"][2]["annotations"]["idempotentHint"] is False


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("read_status", {"tenant_id": "bravo"}),
        ("search_evidence", {"query": "BTC", "tenant_id": "bravo"}),
        ("search_evidence", {"query": 123}),
        ("search_evidence", {}),
        ("submit_research", {"question": "BTC", "auto_execute": True}),
        ("submit_research", {"question": "BTC", "tenant_id": "bravo"}),
        ("submit_research", {"question": ""}),
    ],
)
def test_tool_input_schema_enforced_before_dispatch(
    client: TestClient, runtime: HubRuntime, name: str, arguments: dict
) -> None:
    body, headers = request("tools/call", {"name": name, "arguments": arguments})
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == -32602
    assert runtime.store.list_jobs("alpha") == []


def test_research_tool_can_never_enable_execution(client: TestClient, runtime: HubRuntime) -> None:
    body, headers = request(
        "tools/call", {"name": "submit_research", "arguments": {"question": "Review BTC"}}
    )
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    job = runtime.store.list_jobs("alpha")[0]
    assert job["payload"]["auto_execute"] is False
    assert runtime.trading.orders("alpha") == []


def test_legacy_initialization_and_json_responses_remain_available(client: TestClient) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": LEGACY},
    }
    result = client.post("/mcp", headers=AUTH, json=body).json()["result"]
    assert result["protocolVersion"] == LEGACY and "resultType" not in result
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = client.post("/mcp", headers=AUTH, json=notification)
    assert response.status_code == 202 and not response.content
    result = client.post(
        "/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert result.status_code == 200 and len(result.json()["result"]["tools"]) == 3


@pytest.mark.parametrize(
    "payload,code",
    [
        ("{not json", -32700),
        ("[]", -32600),
        ('{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600),
    ],
)
def test_malformed_json_and_batch_requests_fail_closed(
    client: TestClient, payload: str, code: int
) -> None:
    response = client.post(
        "/mcp", headers={**AUTH, "Content-Type": "application/json"}, content=payload
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == code


def test_http_auth_origin_and_method_boundary_remain_enforced(client: TestClient) -> None:
    body, headers = request("server/discover")
    no_auth = {key: value for key, value in headers.items() if key != "Authorization"}
    assert client.post("/mcp", headers=no_auth, json=body).status_code == 401
    assert (
        client.post(
            "/mcp", headers={**headers, "Origin": "https://untrusted.example"}, json=body
        ).status_code
        == 403
    )
    assert client.get("/mcp", headers=AUTH).status_code == 405
