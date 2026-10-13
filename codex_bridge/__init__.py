"""Bridge public interface; importing the package does not read configuration or start clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_bridge.codex.codex_execution import Codex, LiveTurn


__all__ = ["Codex", "LiveTurn"]


def __getattr__(name: str) -> type[Codex] | type[LiveTurn]:
    """首次访问公共类时才加载配置及 SDK 集成。"""
    if name in __all__:
        from codex_bridge.codex import codex_execution as codex
        value = getattr(codex, name)
        globals()[name] = value
        return value
    raise AttributeError(f"Module {__name__!r} has no attribute {name!r}")
