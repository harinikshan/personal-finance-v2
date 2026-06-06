"""
perception.py — the Perception cognitive layer.

Input:  a raw user message (str).
Output: a typed PerceptionOutput.

It classifies the request type(s), extracts the structured FinancialContext the
message states, lists what's missing, flags any facts the user asked us to
remember, and decides whether answering needs facts from a prior run. The call
goes through LLM Gateway V3 with auto_route="perception" and a json_schema
response format, so the worker output is schema-validated — never regex-parsed.
"""

from __future__ import annotations

import gateway
from schemas import REQUEST_TYPES, PerceptionOutput

_SYSTEM = (
    "You are the PERCEPTION layer of a structured AI Personal Finance Agent. "
    "Read ONE user message and extract structured information. Do not give "
    "financial advice and do not do any calculations here.\n\n"
    "Tasks:\n"
    f"1. request_types: choose all that apply from this exact list: {REQUEST_TYPES}.\n"
    "2. context: fill FinancialContext fields with EVERY number/attribute the "
    "message states; leave only genuinely-unstated fields null. Money values are "
    "plain numbers, dropping currency symbols and Indian-format commas: "
    "'Rs 1,20,000' -> 120000, 'Rs 70,000' -> 70000, '1.5 lakh' -> 150000, "
    "'5 crore' -> 50000000. If the user gives income, expenses or savings, you "
    "MUST capture them. risk_appetite must be one of conservative|moderate|aggressive.\n"
    "3. missing_information: list the inputs you'd still need to answer well.\n"
    "4. facts_to_remember: ONLY when the user explicitly says to remember / save / "
    "note something for the future, capture each as {key, value}. Otherwise empty.\n"
    "5. needs_recall: true if the question references things the user told you "
    "before ('what you know about me', 'my goal', 'my risk appetite') without "
    "restating them in this message.\n"
)


def perceive(query: str) -> PerceptionOutput:
    out, _resp = gateway.structured(
        PerceptionOutput,
        prompt=query,
        system=_SYSTEM,
        auto_route="perception",
    )
    # Keep only request types from the canonical list (defensive, not regex on prose).
    out.request_types = [r for r in out.request_types if r in REQUEST_TYPES]
    return out
