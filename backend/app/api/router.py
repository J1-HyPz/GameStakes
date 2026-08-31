"""Aggregate API router. One sub-router per resource, mounted under /api."""

from fastapi import APIRouter

from app.api import catalog, health, resolution

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(catalog.router)
api_router.include_router(resolution.router)
