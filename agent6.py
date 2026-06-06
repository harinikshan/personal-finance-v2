"""
agent6.py — wires the four cognitive layers into a Perception -> Memory ->
Decision -> Action loop.

    perceive(query)                      # Perception: structured reading
    remember(...) + recall(...)          # Memory:     durable persistence + recall
    loop: decide(...) -> execute(...)    # Decision + Action: the agentic loop
    synthesize(...)                      # Decision:   structured FINAL_RESPONSE

Run a single query:
    uv run python agent6.py "How big should my emergency fund be ..."

Run one of the four built-in target queries (A, B, C, D):
    uv run python agent6.py --query A
    uv run python agent6.py --query C1     # Query C, run 1 (records the fact)
    uv run python agent6.py --query C2     # Query C, run 2 (reads it back)

State persists under state/. Wipe it between attempts:  rm -rf state/ sandbox/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import action
import decision
import memory
import perception
from gateway import GatewayError
from schemas import AgentResult, FinancialContext, PlannedToolCall

MAX_ITERATIONS = 10
TOOL_RESULT_CHAR_CAP = 2000  # keep tool results small so the decision loop stays in TINY/LARGE tiers

# The four target queries. C is two runs that share durable memory.
TARGET_QUERIES = {
    "A": (
        "I'm 30 years old in India. I earn Rs 1,20,000 per month and my expenses "
        "are Rs 70,000 per month. I have Rs 1,50,000 in savings but no dedicated "
        "emergency fund. How big should my emergency fund be, how many months of "
        "my surplus will it take to build it, and how much can I then invest in a "
        "SIP each month? Assume the SIP earns 12% per year; project the corpus "
        "after 10 years."
    ),
    "B": (
        "I want to take a home loan of Rs 20,00,000 at 9% annual interest for 20 "
        "years. What will my monthly EMI be? Also, my uncle in the US wants to "
        "gift me USD 5,000 toward the down payment - how much is that in INR at "
        "today's exchange rate?"
    ),
    "C1": (
        "Please remember the following about me for future sessions: my risk "
        "appetite is aggressive, my retirement goal is a corpus of Rs 5 crore by "
        "age 60, and I am 30 years old now."
    ),
    "C2": (
        "Based on what you already know about me, how much do I need to invest "
        "every month to reach my retirement goal, assuming my investments earn "
        "12% per year? Show the required monthly SIP."
    ),
    "D": (
        "What is the current EPF (Employees' Provident Fund) interest rate in "
        "India for FY 2024-25? Using that rate, if I contribute Rs 12,500 per "
        "month for 25 years, what retirement corpus would I accumulate?"
    ),
}


def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _clip(value, cap: int = TOOL_RESULT_CHAR_CAP) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(s) > cap:
        return s[:cap] + f" ...[truncated {len(s) - cap} chars]"
    return s


def _cap_result(result):
    """Keep tool results small before they enter the transcript/scratchpad, so
    the single local worker's context never balloons (e.g. fetch_url markdown).
    finance_calc results are tiny and pass through unchanged."""
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 800:
                out[k] = v[:800] + f" ...[+{len(v) - 800} chars]"
            else:
                out[k] = v
        return out
    if isinstance(result, str) and len(result) > 1200:
        return result[:1200] + f" ...[+{len(result) - 1200} chars]"
    return result


def run_query(query: str, run_id: str) -> AgentResult:
    _hr(f"QUERY  (run_id={run_id})")
    print(query)

    # ---------------- Perception ----------------
    percept = perception.perceive(query)
    _hr("PERCEPTION  (auto_route=perception, schema-validated)")
    print(percept.model_dump_json(indent=2))

    # ---------------- Memory ----------------
    store = memory.load_store()
    written = memory.remember(
        percept.facts_to_remember, store, source_query=query, run_id=run_id
    )
    recall = memory.recall(query, store)
    _hr("MEMORY  (durable store under state/memory.json, auto_route=memory)")
    if written:
        print(f"Persisted {len(written)} fact(s): " + ", ".join(f.key for f in written))
    print(f"Durable store now holds: {[f.key for f in store.facts]}")
    if recall.relevant_facts:
        print("Recalled for this query:")
        for f in recall.relevant_facts:
            print(f"  - {f}")
    context: FinancialContext = percept.context.merged_with(recall.context_patch)

    # ---------------- Decision + Action loop ----------------
    host = action.MCPToolHost()
    tool_names = [t.name for t in host.list_tools()]
    _hr("TOOLS available from MCP server (stdio)")
    print(", ".join(tool_names))

    transcript: list[dict] = []
    tool_calls_made: list[str] = []
    iterations = 0
    last_reasoning = ""
    signatures: set[str] = set()
    repeats = 0

    _hr("DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)")
    while iterations < MAX_ITERATIONS:
        iterations += 1
        step, meta = decision.decide(
            query=query, context=context, recalled_facts=recall.relevant_facts,
            tool_names=tool_names, scratchpad=transcript,
        )
        last_reasoning = step.thought or last_reasoning
        tier = f"[{meta.get('tier')}->{meta.get('provider')}:{meta.get('model')}]"
        print(f"\n-- iteration {iterations} {tier}")
        if step.thought.strip():
            print(f"   thought: {step.thought.strip()[:300]}")

        if step.action == "finalize":
            print("   decision: finalize -> synthesize answer")
            break
        if not step.tool_name or step.tool_name not in tool_names:
            # Unknown/empty tool: record and let the next turn correct course.
            transcript.append({
                "tool": str(step.tool_name), "arguments": step.tool_args,
                "result": {"error": f"unknown tool '{step.tool_name}'"},
            })
            print(f"   tool: INVALID tool_name={step.tool_name!r} -> fed back as error")
            continue

        # Defensive: small local models sometimes echo notation like 'months?'
        # as a key. Drop keys ending in '?'. (Belt-and-suspenders; the prompt
        # already forbids them.)
        args = {k.rstrip("?"): v for k, v in step.tool_args.items()}

        # Loop guard: if the planner keeps requesting an identical call, stop and
        # synthesize from what we already have rather than spin to MAX.
        sig = f"{step.tool_name}:{json.dumps(args, sort_keys=True)}"
        if sig in signatures:
            repeats += 1
            print(f"   (repeat of an earlier call — guard {repeats}/2)")
            if repeats >= 2:
                print("   decision: repeated calls detected -> finalize early")
                break
            continue
        signatures.add(sig)

        call = PlannedToolCall(id=f"call_{iterations}", name=step.tool_name,
                               arguments=args)
        result = host.execute(call)
        tool_calls_made.append(call.name)
        status = "ok" if result.ok else f"ERROR: {result.error}"
        payload = result.result if result.ok else result.error
        print(f"   tool: {call.name}({json.dumps(call.arguments)})")
        print(f"         -> {status}: {_clip(payload, 400)}")
        transcript.append({
            "tool": call.name,
            "arguments": call.arguments,
            "result": _cap_result(result.result) if result.ok else {"error": result.error},
        })

    # ---------------- Final synthesis ----------------
    host.close()
    final = decision.synthesize(
        query=query,
        context=context,
        recalled_facts=recall.relevant_facts,
        transcript=transcript,
        last_reasoning=last_reasoning,
    )
    _hr(f"FINAL_RESPONSE  (iterations={iterations}, tools={tool_calls_made})")
    print(final.model_dump_json(indent=2))

    return AgentResult(
        query=query,
        run_id=run_id,
        iterations=iterations,
        tool_calls_made=tool_calls_made,
        perception=percept,
        recalled_facts=recall.relevant_facts,
        final=final,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Personal Finance Agent (Session 6)")
    ap.add_argument("query", nargs="?", help="a free-form financial question")
    ap.add_argument("--query", dest="canned", choices=sorted(TARGET_QUERIES),
                    help="run a built-in target query (A, B, C1, C2, D)")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    if args.canned:
        query = TARGET_QUERIES[args.canned]
        run_id = args.run_id or f"{args.canned}"
    elif args.query:
        query = args.query
        run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
    else:
        ap.error("provide a query string or --query {A,B,C1,C2,D}")
        return 2

    try:
        run_query(query, run_id)
    except GatewayError as e:
        print(f"\n[gateway error] {e}\n\nIs LLM Gateway V3 running on :8101?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
