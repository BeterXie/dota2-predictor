"""Dota 2 live-data collection and shadow betting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


__all__ = ["LiveEvent", "LiveFrame", "Market", "OddsSnapshot", "ShadowOrder"]

if TYPE_CHECKING:
    from .models import LiveEvent, LiveFrame, Market, OddsSnapshot, ShadowOrder


def __getattr__(name: str) -> Any:
    """Keep public model exports without importing them during package bootstrap."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import models

    value = getattr(models, name)
    globals()[name] = value
    return value
