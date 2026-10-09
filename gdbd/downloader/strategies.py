"""Registry mapping a stored strategy name to its implementation."""

from __future__ import annotations

from ..drive.exporters import (
    STRATEGY_BINARY,
    STRATEGY_EXPORT,
    STRATEGY_SHORTCUT,
    STRATEGY_UNSUPPORTED,
)
from .base import DownloadStrategy
from .binary import BinaryMediaDownloader
from .special import ShortcutHandler, SpecialFileDownloader
from .workspace import WorkspaceExportDownloader

#: One shared, stateless instance per strategy - they hold no per-item state.
_REGISTRY: dict[str, DownloadStrategy] = {
    STRATEGY_BINARY: BinaryMediaDownloader(),
    STRATEGY_EXPORT: WorkspaceExportDownloader(),
    STRATEGY_SHORTCUT: ShortcutHandler(),
    STRATEGY_UNSUPPORTED: SpecialFileDownloader(),
}


def get_strategy(name: str) -> DownloadStrategy:
    """Return the strategy for ``name``, defaulting to the binary downloader."""
    return _REGISTRY.get(name, _REGISTRY[STRATEGY_BINARY])


def register_strategy(name: str, strategy: DownloadStrategy) -> None:
    """Add or replace a strategy (used by tests and future file types)."""
    _REGISTRY[name] = strategy


def strategy_names() -> list[str]:
    """Every registered strategy name."""
    return sorted(_REGISTRY)
