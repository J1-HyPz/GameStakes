"""Aggregate API router. One sub-router per resource, mounted under /api."""

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
