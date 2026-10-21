"""Authenticated MCP tools over JSON HTTP, modern 2026-07-28 + legacy compatibility.

Normative sources (checked 2026-10-21):
https://modelcontextprotocol.io/specification/2026-07-28/basic
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
https://modelcontextprotocol.io/specification/2026-07-28/server/discover
https://modelcontextprotocol.io/specification/2026-07-28/server/tools

This bounded server supports discovery and three tools, not arbitrary schema/tool
registration, subscriptions, Tasks, sampling, OAuth discovery, or A2A. Authentication
and Origin validation belong to the surrounding API and apply before dispatch.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from typing import Any

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
SUPPORTED = [MODERN, LEGACY]
PREFIX = "io.modelcontextprotocol/"
SERVER_INFO = {"name": "qt-agent-hub", "version": "2.0.0"}


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchArguments(EmptyArguments):
    query: str = Field(min_length=1, max_length=4096)


class ResearchArguments(EmptyArguments):
    question: str = Field(min_length=1, max_length=8000)


ARGUMENTS = {
    "search_evidence": SearchArguments,
    "read_status": EmptyArguments,
    "submit_research": ResearchArguments,
}


def tool_definitions() -> list[dict]:
    descriptions = {
        "search_evidence": "Search the authenticated owner's currently available evidence",
        "read_status": "Read the authenticated owner's task, worker and trading status",
        "submit_research": "Enqueue a research-only workflow; automatic trading is disabled",
    }
    return [
        {
            "name": name,
            "description": descriptions[name],
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **model.model_json_schema(),
            },
            "annotations": {
                "readOnlyHint": name != "submit_research",
                "destructiveHint": False,
                "idempotentHint": name != "submit_research",
                "openWorldHint": False,
            },
        }
        for name, model in ARGUMENTS.items()
    ]


def error(
    request_id: Any, code: int, message: str, *, status: int = 400, data: dict | None = None
) -> JSONResponse:
    payload: dict = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if type(request_id) in (str, int):
        payload["id"] = request_id
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(payload, status_code=status)


def _decode_header(value: str | None, *, encoded: bool = False) -> str:
    if (
        not value
        or value != value.strip()
        or any(ord(char) not in range(0x20, 0x7F) and char != "\t" for char in value)
    ):
        raise ValueError("malformed header")
    if encoded and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("malformed encoded header") from None
    return value


def dispatch(
    body: object,
    headers: Mapping[str, str],
    *,
    search: Callable[[str], object],
    status: Callable[[], dict],
    submit_research: Callable[[str], dict],
) -> Response:
    """Dispatch one request, deriving era and capabilities from this request only."""
    if not isinstance(body, dict):
        return error(None, -32600, "A single JSON-RPC request object is required")
    request_id, method, params = body.get("id"), body.get("method"), body.get("params", {})
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        modern_header = headers.get("mcp-protocol-version") not in (None, LEGACY)
        return error(
            request_id,
            -32600,
            "Invalid request",
            status=400 if modern_header or method == "server/discover" else 200,
        )
    meta = params.get("_meta", {})
    header_version = headers.get("mcp-protocol-version")
    body_version = meta.get(PREFIX + "protocolVersion") if isinstance(meta, dict) else None
    # No protocol session exists. Legacy requests without version headers remain
    # accepted only as the pre-existing local compatibility surface.
    modern = (
        method == "server/discover"
        or (header_version is not None and header_version != LEGACY)
        or (
            isinstance(meta, dict)
            and any(
                PREFIX + key in meta
                for key in ("protocolVersion", "clientCapabilities", "clientInfo")
            )
        )
    )
    if "id" not in body:
        if not modern and method == "notifications/initialized":
            return Response(status_code=202)
        return error(None, -32600, "No client notifications are supported on this HTTP transport")
    if type(request_id) not in (str, int):
        return error(None, -32600, "Request ID must be a string or integer")

    if modern:
        if (
            not isinstance(meta, dict)
            or not isinstance(body_version, str)
            or not 1 <= len(body_version) <= 64
            or not isinstance(meta.get(PREFIX + "clientCapabilities"), dict)
        ):
            return error(
                request_id, -32602, "Required per-request protocol metadata is missing or invalid"
            )
        client = meta.get(PREFIX + "clientInfo")
        if client is not None and (
            not isinstance(client, dict)
            or any(
                not isinstance(client.get(key), str) or not 1 <= len(client[key]) <= 200
                for key in ("name", "version")
            )
        ):
            return error(request_id, -32602, "Invalid clientInfo")
        try:
            if _decode_header(header_version) != body_version:
                raise ValueError("version mismatch")
            if _decode_header(headers.get("mcp-method")) != method:
                raise ValueError("method mismatch")
            if method in {"tools/call", "resources/read", "prompts/get"}:
                source = params.get("uri") if method == "resources/read" else params.get("name")
                if _decode_header(headers.get("mcp-name"), encoded=True) != source:
                    raise ValueError("name mismatch")
        except ValueError:
            return error(
                request_id,
                -32020,
                "Required HTTP metadata headers are missing, malformed or mismatched",
            )
        if body_version != MODERN:
            return error(
                request_id,
                -32022,
                "Unsupported per-request protocol version",
                data={"supported": SUPPORTED, "requested": body_version},
            )
    else:
        requested = params.get("protocolVersion", LEGACY) if method == "initialize" else LEGACY
        if requested != LEGACY:
            return error(
                request_id,
                -32022,
                "Unsupported legacy protocol version",
                data={"supported": SUPPORTED, "requested": str(requested)[:64]},
            )

    def success(result: dict) -> JSONResponse:
        if modern:
            result = {
                **result,
                "resultType": "complete",
                "_meta": {PREFIX + "serverInfo": SERVER_INFO},
            }
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    invalid_status = 400 if modern else 200
    if method == "server/discover":
        if set(params) - {"_meta"}:
            return error(request_id, -32602, "Discovery accepts only request metadata")
        return success(
            {
                "supportedVersions": SUPPORTED,
                "capabilities": {"tools": {}},
                "instructions": "Personal evidence and research tools. Tenant identity is server-owned. Research submissions never authorize trading.",
                "ttlMs": 300000,
                "cacheScope": "private",
            }
        )
    if not modern and method == "initialize":
        return success(
            {"protocolVersion": LEGACY, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        )
    if not modern and method == "ping":
        return success({})
    if method == "tools/list":
        if set(params) - {"_meta", "cursor"} or params.get("cursor") is not None:
            return error(
                request_id,
                -32602,
                "Invalid pagination cursor or list parameters",
                status=invalid_status,
            )
        return success(
            {
                "tools": tool_definitions(),
                **({"ttlMs": 300000, "cacheScope": "private"} if modern else {}),
            }
        )
    if method == "tools/call":
        if set(params) - {"_meta", "name", "arguments"}:
            return error(
                request_id, -32602, "Unsupported tool call parameters", status=invalid_status
            )
        name = params.get("name")
        if not isinstance(name, str) or name not in ARGUMENTS:
            return error(request_id, -32602, "Unknown tool", status=invalid_status)
        try:
            arguments = ARGUMENTS[name].model_validate(params.get("arguments", {})).model_dump()
        except ValidationError:
            return error(
                request_id, -32602, "Tool arguments do not match inputSchema", status=invalid_status
            )
        try:
            if name == "search_evidence":
                value = search(arguments["query"])
            elif name == "read_status":
                value = status()
            else:
                value = submit_research(arguments["question"])
        except (ValueError, OSError, RuntimeError):
            return success(
                {"content": [{"type": "text", "text": "Tool execution failed"}], "isError": True}
            )
        return success(
            {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "isError": False,
            }
        )
    return error(request_id, -32601, "Method not found", status=404 if modern else 200)
