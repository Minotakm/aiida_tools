"""AiiDA Error Inspector - a Terminal UI for browsing and tagging failed AiiDA workchains."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["GroupNodesApp", "__version__"]


def __getattr__(name: str):
    """Import the app lazily.

    Importing this package must not drag in AiiDA and Textual: the pure-logic
    modules (storage, classify) are unit-tested without either installed.
    """
    if name == "GroupNodesApp":
        from .app import GroupNodesApp

        return GroupNodesApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
