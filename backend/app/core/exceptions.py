"""Application exception hierarchy.

Domain code raises these; the API layer maps them to HTTP responses in one
place so error shapes stay consistent.
"""


class GameStakesError(Exception):
    """Base class for all application errors."""


class ConfigurationError(GameStakesError):
    """A required setting is missing or invalid for the requested feature."""


class ProviderError(GameStakesError):
    """An external data provider failed or returned an unusable response."""


class NotFoundError(GameStakesError):
    """A requested entity does not exist."""
