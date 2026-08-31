"""Aggregate API router. One sub-router per resource, mounted under /api."""

from fastapi import APIRouter

from app.api import (
    backtest,
    bets,
    catalog,
    fixtures,
    health,
    jobs,
    predictions,
    providers,
    resolution,
    tracker,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(catalog.router)
api_router.include_router(fixtures.router)
api_router.include_router(predictions.router)
api_router.include_router(bets.router)
api_router.include_router(tracker.router)
api_router.include_router(backtest.router)
api_router.include_router(jobs.router)
api_router.include_router(providers.router)
api_router.include_router(resolution.router)
