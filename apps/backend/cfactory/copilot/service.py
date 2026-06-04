"""Agentic pipeline copilot — scaffold (#13).

A read-first LLM layer that reasons over the WorkItem store. ``ask()`` builds a
compact snapshot of the board and asks Claude to answer questions like "where is
feature #142 and why is it stuck".

The actual LLM call goes through a **runner seam** (``AgentRunner``) so tests
stay hermetic — they inject a fake runner and never touch the SDK, network, or
an API key. The default runner is the live integration point (Claude Agent SDK).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..store import WorkItemStore, get_store

# (question, context, system_prompt) -> answer text
AgentRunner = Callable[[str, str, str], str]

SYSTEM_PROMPT = (
    "You are the CFactory pipeline copilot. You observe an autonomous software "
    "factory whose units of work thread across three services — PFactory (plan), "
    "AIFactory (code), TFactory (test) — correlated by GitHub issue number.\n\n"
    "Answer the user's question using ONLY the board snapshot provided. Be concise, "
    "cite correlation keys (e.g. #142), and when a unit looks stuck name the stage "
    "and last status. If the snapshot doesn't contain the answer, say so plainly."
)

_MAX_ITEMS = 100


@dataclass
class CopilotAnswer:
    answer: str
    work_items_considered: int


def build_board_snapshot(store: WorkItemStore, *, limit: int = _MAX_ITEMS) -> str:
    """A compact, LLM-friendly text snapshot of the WorkItem board."""
    items = store.list()[:limit]
    if not items:
        return "(no work items)"
    lines = []
    for wi in items:
        def slot(s):
            return f"{s.status or '-'}" + (f"/{s.phase}" if s.phase else "")
        lines.append(
            f"#{wi.correlation_key} {wi.title or ''}".rstrip()
            + f" | plan(pfactory)={slot(wi.pfactory)}"
            + f" | code(aifactory)={slot(wi.aifactory)}"
            + f" | test(tfactory)={slot(wi.tfactory)}"
            + f" | events={len(wi.timeline)}"
        )
    return "\n".join(lines)


def _default_runner(model: str) -> AgentRunner:
    """Live runner backed by the Claude Agent SDK.

    Isolated behind the seam and lazily imported, so the test suite needs
    neither the SDK nor ANTHROPIC_API_KEY. Verify on a real host.
    """

    def run(question: str, context: str, system_prompt: str) -> str:
        import anyio  # provided transitively by the SDK
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        prompt = f"Board snapshot:\n{context}\n\nQuestion: {question}"

        async def _ask() -> str:
            text_parts: list[str] = []
            options = ClaudeAgentOptions(system_prompt=system_prompt, model=model, max_turns=1)
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    for block in getattr(message, "content", None) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
            return "".join(text_parts).strip() or "(no response)"

        return anyio.run(_ask)

    return run


class Copilot:
    """Read-only Q&A copilot over the WorkItem board."""

    def __init__(
        self,
        store: WorkItemStore,
        *,
        runner: AgentRunner | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self._runner = runner or _default_runner(self._settings.copilot_model)

    def ask(self, question: str) -> CopilotAnswer:
        items = self._store.list()
        snapshot = build_board_snapshot(self._store)
        answer = self._runner(question, snapshot, SYSTEM_PROMPT)
        return CopilotAnswer(answer=answer, work_items_considered=len(items))


_copilot: Copilot | None = None


def get_copilot() -> Copilot:
    global _copilot
    if _copilot is None:
        _copilot = Copilot(get_store())
    return _copilot
