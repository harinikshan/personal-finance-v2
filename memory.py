"""
memory.py — the Memory cognitive layer.

Two responsibilities:
  1. DURABLE persistence. Facts the user asks us to remember are written to
     state/memory.json and survive across process runs. This is what makes
     Query C work: run 1 stores the fact, run 2 reads it.
  2. RECALL. Given the current query, an LLM call (auto_route="memory" through
     LLM Gateway V3) selects which stored facts are relevant and re-expresses
     them as a typed FinancialContext patch. No regex, no free-form dicts.

The store lives under state/ so it can be wiped between assignment attempts
(`rm -rf state/`) and is excluded from git via .gitignore.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import gateway
from schemas import (
    MemoryDirective,
    MemoryFact,
    MemoryRecallOutput,
    MemoryStore,
)

STATE_DIR = Path(__file__).parent / "state"
MEMORY_PATH = STATE_DIR / "memory.json"


# --------------------------------------------------------------------------- #
# Durable store I/O
# --------------------------------------------------------------------------- #
def load_store() -> MemoryStore:
    if not MEMORY_PATH.exists():
        return MemoryStore()
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        return MemoryStore.model_validate(data)
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupt store should never crash a run; start clean but don't delete.
        return MemoryStore()


def save_store(store: MemoryStore) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(store.model_dump_json(indent=2), encoding="utf-8")


def remember(
    directives: list[MemoryDirective],
    store: MemoryStore,
    *,
    source_query: str,
    run_id: str,
) -> list[MemoryFact]:
    """Persist perception's facts_to_remember into the durable store."""
    if not directives:
        return []
    now = datetime.now(timezone.utc).isoformat()
    written: list[MemoryFact] = []
    for d in directives:
        fact = MemoryFact(
            key=d.key,
            value=d.value,
            source_query=source_query,
            run_id=run_id,
            stored_at=now,
        )
        store.upsert(fact)
        written.append(fact)
    save_store(store)
    return written


# --------------------------------------------------------------------------- #
# LLM-backed recall
# --------------------------------------------------------------------------- #
_RECALL_SYSTEM = (
    "You are the MEMORY layer of a personal-finance agent. You are given the "
    "user's current question and a list of durable facts remembered from earlier "
    "sessions. Select only the facts relevant to answering the current question "
    "and re-express any usable numbers/attributes as structured FinancialContext "
    "fields in context_patch. If a remembered fact implies a time horizon "
    "(e.g. 'retire at 60' and 'I am 30 now'), compute time_horizon_years. "
    "Do not invent facts that are not in the stored list."
)


def recall(query: str, store: MemoryStore) -> MemoryRecallOutput:
    """Use the memory-tier LLM to pick relevant durable facts for this query.

    Returns an empty recall (no LLM call) when the store is empty, so first-ever
    runs don't pay for a pointless gateway call.
    """
    if not store.facts:
        return MemoryRecallOutput()

    facts_block = "\n".join(f"- {f.key}: {f.value}" for f in store.facts)
    prompt = (
        f"CURRENT QUESTION:\n{query}\n\n"
        f"DURABLE FACTS FROM EARLIER SESSIONS:\n{facts_block}\n\n"
        "Return the relevant facts and a context_patch."
    )
    try:
        recall_out, _resp = gateway.structured(
            MemoryRecallOutput,
            prompt=prompt,
            system=_RECALL_SYSTEM,
            auto_route="memory",
        )
    except Exception:
        recall_out = MemoryRecallOutput()

    # The LLM relevance pass is best-effort. A small local worker sometimes
    # returns an empty selection; never let that silently drop durable facts the
    # answer depends on. Fall back to surfacing every stored fact verbatim so the
    # decision layer always sees them.
    if not recall_out.relevant_facts:
        recall_out.relevant_facts = [f"{f.key}: {f.value}" for f in store.facts]
    return recall_out
