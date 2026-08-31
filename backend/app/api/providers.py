"""Data-source endpoints for the settings page: what is configured, what each
source unlocks, and a live connection test per provider.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.providers.base import ProviderHealth
from app.providers.registry import describe_providers, get_registry

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderInfo(BaseModel):
    name: str
    configured: bool
    best_effort: bool
    sports: list[str]
    leagues: list[str]
    capabilities: list[str]
    priority: int


@router.get("")
async def list_providers() -> list[ProviderInfo]:
    """Static description — no upstream calls, so this is always fast."""
    return [ProviderInfo(**p) for p in describe_providers(get_registry())]


@router.post("/{name}/test")
async def test_provider(name: str) -> ProviderHealth:
    """Live connection test. Health checks avoid metered endpoints, so this is
    safe to press repeatedly."""
    provider = get_registry().by_name(name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{name}'")
    return await provider.health()


@router.get("/health")
async def providers_health() -> list[ProviderHealth]:
    return await get_registry().health()
