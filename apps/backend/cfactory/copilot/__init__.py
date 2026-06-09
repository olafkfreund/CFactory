"""Agentic pipeline copilot (Epic #2)."""

from .service import (
    Copilot,
    CopilotAnswer,
    build_board_snapshot,
    get_copilot,
    make_runner,
    provider_status,
    reset_copilot,
)

__all__ = [
    "Copilot",
    "CopilotAnswer",
    "build_board_snapshot",
    "get_copilot",
    "make_runner",
    "provider_status",
    "reset_copilot",
]
