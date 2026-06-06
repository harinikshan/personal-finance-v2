"""
decision.py — the Decision cognitive layer.

Every call goes through LLM Gateway V3 with auto_route="decision". The layer
plans ONE step at a time as schema-validated structured output (a typed
NextAction), so it does not depend on a worker's native tool-calling quality —
which matters when the only available worker is a small local model. The chosen
tool is then dispatched by the Action layer over MCP. No regex is run on model
output: the gateway constrains generation to the NextAction / FinalResponse
JSON schema and returns the validated object.

  decide(...)     -> (NextAction, routing_meta)   # plan the next step
  synthesize(...) -> FinalResponse                # structured final answer
"""

from __future__ import annotations

import json
from typing import Any

import gateway
from schemas import FinalResponse, FinancialContext, NextAction

# Concise call hints for the tools the planner is most likely to use. Anything
# the MCP server advertises but isn't listed here still appears by name.
_TOOL_HINTS: dict[str, str] = {
    "finance_calc": (
        'do ALL arithmetic here. tool_args = {"operation": <op>, "params": {...}}. '
        'Pass ONLY the listed param keys (no extra keys, no "?" characters). ops:\n'
        '      surplus -> params {monthly_income, monthly_expenses};\n'
        '      emergency_fund -> params {monthly_expenses, months}  (months defaults to 6; for "6 months expenses" pass months=6);\n'
        '      months_to_goal -> params {target, current, monthly_contribution};\n'
        '      sip_future_value -> params {monthly, annual_rate_pct, years};\n'
        '      required_sip -> params {target, annual_rate_pct, years};\n'
        '      emi -> params {principal, annual_rate_pct, years};\n'
        '      compound -> params {principal, annual_rate_pct, years};\n'
        '      cagr -> params {begin_value, end_value, years};\n'
        '      inflation_adjust -> params {amount, inflation_pct, years}.\n'
        '      rates are PERCENT per year (9 means 9%).'
    ),
    "currency_convert": 'live FX. tool_args = {"amount": 5000, "from_currency": "USD", "to_currency": "INR"}.',
    "web_search": 'web search for live data (rates/prices). tool_args = {"query": "...", "max_results": 3}.',
    "fetch_url": 'fetch one page as markdown. tool_args = {"url": "https://..."}.',
    "get_time": 'current date/time. tool_args = {"timezone": "Asia/Kolkata"}.',
}

DECISION_SYSTEM = (
    "You are the DECISION layer of a structured AI Personal Finance Agent. You "
    "drive an agentic loop ONE step at a time and must answer with a single "
    "NextAction object.\n\n"
    "Rules:\n"
    "- Keep the 'thought' field to ONE short sentence (under 20 words).\n"
    "- Think step by step; never conclude without doing the math.\n"
    "- ALL arithmetic MUST go through the finance_calc tool. Never compute "
    "numbers yourself.\n"
    "- Take exactly ONE action per turn. Look at WORK DONE SO FAR: do not repeat "
    "a tool call that already has a result.\n"
    "- When every number the question asks for has already been computed in WORK "
    "DONE SO FAR, choose action='finalize'. Otherwise choose action='call_tool'.\n"
    "- If (and only if) action='call_tool', you MUST set a real tool_name and "
    "tool_args. Never emit call_tool with an empty tool_name — if there is "
    "nothing left to call, use action='finalize'.\n"
    "- A web_search result already containing the fact you needed is enough; do "
    "not search again — extract the number and move on.\n"
    "- NEVER invent inputs. Use only numbers present in the USER QUESTION, KNOWN "
    "CONTEXT or RECALLED FROM MEMORY. Do not fabricate income, expenses, savings "
    "or any figure the user did not give.\n"
    "- If the user ONLY asked you to remember/store/note facts for later (and is "
    "NOT asking for any calculation or number), there is nothing to compute: "
    "choose action='finalize' immediately on the first turn.\n"
    "- Be conservative; separate facts from assumptions; never guarantee returns."
)


def _catalog(tool_names: list[str]) -> str:
    lines = []
    for name in tool_names:
        hint = _TOOL_HINTS.get(name)
        if hint:
            lines.append(f"- {name}: {hint}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def decide(
    *,
    query: str,
    context: FinancialContext,
    recalled_facts: list[str],
    tool_names: list[str],
    scratchpad: list[dict[str, Any]],
) -> tuple[NextAction, dict[str, Any]]:
    known = {k: v for k, v in context.model_dump().items() if v is not None}
    if scratchpad:
        work = "\n".join(
            f"{i+1}. {s['tool']}({json.dumps(s['arguments'])}) -> {json.dumps(s['result'])}"
            for i, s in enumerate(scratchpad)
        )
    else:
        work = "(nothing yet)"

    prompt = (
        f"USER QUESTION:\n{query}\n\n"
        f"KNOWN CONTEXT: {json.dumps(known)}\n"
        f"RECALLED FROM MEMORY: {recalled_facts}\n\n"
        f"TOOLS YOU CAN CALL:\n{_catalog(tool_names)}\n\n"
        f"WORK DONE SO FAR:\n{work}\n\n"
        "Decide the single next step as a NextAction."
    )

    action, resp = gateway.structured(
        NextAction,
        prompt=prompt,
        system=DECISION_SYSTEM,
        auto_route="decision",
        temperature=0.1,
        max_tokens=1200,
    )
    rd = resp.get("router_decision") or {}
    meta = {
        "tier": rd.get("tier"),
        "provider": resp.get("provider"),
        "model": resp.get("model"),
        "fallback_router": rd.get("fallback_used"),
    }
    return action, meta


_SYNTH_SYSTEM = (
    "You are the DECISION layer producing the FINAL_RESPONSE of a personal "
    "finance agent. Use ONLY the numbers already computed by the finance tools in "
    "the transcript — do not recompute or invent figures. numeric_answer is the "
    "single headline result (include the actual number). Be specific and "
    "conservative. Populate EVERY section with concrete, useful items: "
    "key_findings, risks, recommended_actions, priority_order, short_term_plan, "
    "long_term_plan and assumptions must each contain at least 2-3 entries — do "
    "not leave any of these lists empty. reasoning_types is drawn from: "
    "Arithmetic, Risk Analysis, Comparative Analysis, Forecasting, Constraint "
    "Optimization, Scenario Planning."
)


def synthesize(
    *,
    query: str,
    context: FinancialContext,
    recalled_facts: list[str],
    transcript: list[dict[str, Any]],
    last_reasoning: str,
) -> FinalResponse:
    lines = [f"USER QUESTION:\n{query}\n"]
    known = {k: v for k, v in context.model_dump().items() if v is not None}
    lines.append(f"KNOWN CONTEXT: {json.dumps(known)}")
    if recalled_facts:
        lines.append(f"RECALLED FROM MEMORY: {recalled_facts}")
    lines.append("\nTOOL TRANSCRIPT (computed results — authoritative):")
    for step in transcript:
        lines.append(f"- {step['tool']}({json.dumps(step['arguments'])}) -> {json.dumps(step['result'])}")
    if last_reasoning.strip():
        lines.append(f"\nAGENT NOTES: {last_reasoning.strip()}")
    lines.append("\nNow produce the structured FINAL_RESPONSE.")
    prompt = "\n".join(lines)

    # The final synthesis is one big structured object; some workers drop it
    # mid-generation or 502. Try the normal routing first, then explicitly fail
    # over to a provider with strong native structured-output support, and only
    # then degrade to the deterministic assembly. The computed numbers are
    # authoritative and already in the transcript, so we never lose them.
    attempts = [
        {"auto_route": "decision"},   # honours AGENT_PROVIDER pin / normal routing
        {"provider": "gemini"},       # reliable native JSON
        {"provider": "groq"},         # fast secondary fallback
    ]
    last_err = ""
    for kw in attempts:
        try:
            final, _resp = gateway.structured(
                FinalResponse,
                prompt=prompt,
                system=_SYNTH_SYSTEM,
                max_tokens=2048,
                **kw,
            )
            return final
        except Exception as e:  # noqa: BLE001 — try the next synthesis route
            last_err = str(e)
    return _fallback_final(query, transcript, last_err)


def _fallback_final(query: str, transcript: list[dict[str, Any]], err: str) -> FinalResponse:
    computed = [f"{s['tool']}({json.dumps(s['arguments'])}) = {json.dumps(s['result'])}"
                for s in transcript if isinstance(s.get("result"), dict)
                and "error" not in s["result"]]
    headline = computed[-1] if computed else "see computed values"
    return FinalResponse(
        numeric_answer=headline,
        summary=(
            "Synthesis worker was unavailable; this answer is assembled directly "
            "from the authoritative tool results computed during the run."
        ),
        key_findings=computed or ["No tool results were produced."],
        risks=["Final narrative synthesis was unavailable; figures above are exact "
               "tool outputs, but qualitative risk analysis is omitted."],
        recommended_actions=["Re-run the query once the LLM worker is available for "
                             "a full narrative response."],
        assumptions=[f"Synthesis fell back due to: {err[:160]}"],
        reasoning_types=["Arithmetic"],
    )
