"""Read-only routes; mounted within the existing workbench authentication boundary."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from .polymarket import PredictionSnapshot, PublicDataError, fetch_snapshot, validate_slug


def build_prediction_market_router() -> APIRouter:
    router = APIRouter()

    @router.get("/prediction-markets/polymarket/{slug}")
    async def polymarket(slug: str, response: Response) -> PredictionSnapshot:
        response.headers["Cache-Control"] = "no-store"
        try:
            validate_slug(slug)
        except PublicDataError:
            raise HTTPException(status_code=422, detail="invalid_market_slug") from None
        try:
            return await fetch_snapshot(slug)
        except PublicDataError:
            raise HTTPException(status_code=502, detail="public_market_data_unavailable") from None

    return router
