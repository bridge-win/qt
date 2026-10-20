"""Credential-isolated HTTP worker for the local Codex CLI."""

from __future__ import annotations

import hmac
import math
import os
import threading

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from qt.agent_hub.codex import CodexBusyError, CodexProvider
from qt.agent_hub.config import ROLES


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=80)
    task: str = Field(min_length=1, max_length=8192)
    context: dict
    model: str | None = Field(default=None, max_length=100)


def create_codex_app() -> FastAPI:
    app = FastAPI(title="QT local Codex executor", docs_url=None, redoc_url=None)
    lock = threading.BoundedSemaphore(1)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, **CodexProvider().readiness()}

    @app.post("/invoke")
    def invoke(body: Invocation, authorization: str = Header(default="")) -> dict:
        token = os.environ.get("QT_AGENT_HUB_EXECUTOR_TOKEN", "")
        if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "executor authentication required")
        if body.role not in ROLES:
            raise HTTPException(422, "unknown agent role")
        if not lock.acquire(blocking=False):
            raise HTTPException(429, "executor busy", headers={"Retry-After": "5"})
        try:
            provider = CodexProvider(
                model=body.model, timeout=int(os.environ.get("QT_AGENT_HUB_CODEX_TIMEOUT", "150"))
            )
            answer = provider.invoke(body.role, body.task, body.context)
            return {
                **answer,
                "_runtime": {
                    "usage": provider.last_usage,
                    "duration_seconds": provider.last_duration,
                },
            }
        except CodexBusyError as error:
            raise HTTPException(
                429, "executor busy", headers={"Retry-After": str(math.ceil(error.retry_after))}
            ) from None
        except (RuntimeError, ValueError, OSError):
            raise HTTPException(
                503, "Codex invocation failed; inspect authentication and executor availability"
            ) from None
        finally:
            lock.release()

    return app
