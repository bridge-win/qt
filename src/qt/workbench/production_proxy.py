"""Read-only production readiness bridge through the existing authenticated edge."""

import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException


def build_production_router() -> APIRouter:
    router = APIRouter()

    @router.get("/production/readiness")
    def readiness():
        origin = os.environ.get("QT_V4_CONTROL_ORIGIN", "")
        token = os.environ.get("QT_V4_SERVICE_TOKEN", "")
        if not origin or not token:
            raise HTTPException(503, "production_control_not_configured")
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise HTTPException(503, "production_control_origin_invalid")
        try:
            with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                response = client.get(origin.rstrip("/") + "/api/v4/readiness",
                                      headers={"Authorization": f"Bearer {token}"})
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("readiness_result_too_large")
                result = response.json()
                if not isinstance(result, dict) or result.get("schema_version") != "2.0":
                    raise ValueError("unexpected_readiness_contract")
                return result
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "production_control_unavailable") from None

    return router
