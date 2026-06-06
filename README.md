# AI Personal Finance Agent — EAGV3 Session 6

A four-layer agent (**Perception → Memory → Decision → Action**) that answers
real personal-finance questions by reasoning step-by-step and calling tools. It
is **not** a single-tool toy: each target query runs a multi-step
decide → act loop, every arithmetic operation is delegated to a deterministic
calculator tool, and one query proves **durable memory across separate runs**.

Every LLM call goes through **LLM Gateway V3** (the course substrate) — no
provider SDK is imported anywhere in this code. Every tool call is dispatched
over an **MCP stdio** server. Every boundary between the four layers is a typed
**Pydantic v2** contract; no free-form dicts are passed between roles and **no
regex is ever run on model output**.

---

## Architecture

```
                ┌───────────────────────────────────────────────┐
   user query → │  PERCEPTION   (perception.py)                  │  auto_route="perception"
                │   raw text → PerceptionOutput (schema-valid)   │  → LLM Gateway V3
                └───────────────────────────────────────────────┘
                                  │ PerceptionOutput
                                  ▼
                ┌───────────────────────────────────────────────┐
                │  MEMORY       (memory.py)                      │  auto_route="memory"
                │   persist facts_to_remember → state/memory.json│  → LLM Gateway V3
                │   recall durable facts → MemoryRecallOutput     │
                └───────────────────────────────────────────────┘
                                  │ FinancialContext (+ recalled facts)
                                  ▼
        ┌───────────────────────────────────────────────────────────────┐
        │   loop (agent6.py)                                              │
        │   ┌──────────────────────────┐   NextAction   ┌──────────────┐ │
        │   │ DECISION  (decision.py)  │ ─────────────► │ ACTION       │ │
        │   │ plan ONE step as a typed │                │ (action.py)  │ │
        │   │ NextAction               │ ◄───────────── │ MCP stdio    │ │
        │   │ auto_route="decision"    │   ActionResult │ tool dispatch│ │
        │   └──────────────────────────┘                └──────────────┘ │
        │            │ action=="finalize"                                 │
        └────────────┼────────────────────────────────────────────────────┘
                     ▼
        DECISION synthesize → FinalResponse (the structured FINAL_RESPONSE)
```

### The four cognitive layers and their typed contracts

| Layer | File | LLM via gateway | Input contract | Output contract |
|---|---|---|---|---|
| **Perception** | `perception.py` | `auto_route="perception"` + `response_format` | `str` (user message) | `PerceptionOutput` |
| **Memory** | `memory.py` | `auto_route="memory"` + `response_format` | `str` + `MemoryStore` | `MemoryRecallOutput`; persists `MemoryFact` to `state/memory.json` |
| **Decision** | `decision.py` | `auto_route="decision"` + `response_format` | query + `FinancialContext` + scratchpad | `NextAction`, then `FinalResponse` |
| **Action** | `action.py` | — (no LLM) | `PlannedToolCall` | `ActionResult` (via MCP `call_tool`) |

All contracts live in `schemas.py` (Pydantic v2, `extra="forbid"` on every model).

### Why structured output instead of native tool-calls

The Decision layer plans **one step at a time** as a schema-validated
`NextAction` (`action: call_tool | finalize` + `tool_name` + `tool_args`) rather
than relying on a worker's native tool-calling. This keeps the loop robust on
any worker the gateway routes to — including a single small local model — while
staying fully typed and regex-free. The chosen tool is then executed by the
Action layer using the MCP `ClientSession.call_tool` machinery (tool dispatch is
**not** reimplemented).

---

## Files

| File | Role |
|---|---|
| `schemas.py` | All Pydantic v2 contracts (the boundaries). |
| `perception.py` | Perception layer. |
| `memory.py` | Memory layer + durable `state/memory.json`. |
| `decision.py` | Decision layer (step planner + final synthesis). |
| `action.py` | MCP stdio client / tool dispatch. |
| `agent6.py` | Wires the loop; CLI entry point. |
| `gateway.py` | Thin client to LLM Gateway V3 — the **only** path to an LLM. |
| `mcp_server.py` | MCP server (stdio): 9 original tools **+ `finance_calc`**. |
| `pyproject.toml` | uv-managed dependencies. |
| `state/` | Durable memory (git-ignored, wipeable). |
| `sandbox/`, `usage.json` | MCP file-tool sandbox + search ledger (git-ignored). |

### The `finance_calc` MCP tool

A deterministic calculator added to the MCP server so financial math is
**reproducible** and never hallucinated. Operations: `surplus`,
`emergency_fund`, `months_to_goal`, `sip_future_value`, `required_sip`, `emi`,
`compound`, `cagr`, `inflation_adjust`. The Decision layer is instructed to route
**all** arithmetic through it.

---

## Prerequisites

1. **LLM Gateway V3** running on `:8101` (the substrate for every LLM call):
   ```bash
   cd /path/to/llm_gatewayV3 && ./run.sh
   curl -s http://localhost:8101/v1/routers   # sanity check
   ```
   The gateway reads provider keys from its own `../.env`. With cloud keys
   present it routes perception/memory/decision across the worker pool; with
   only local **Ollama** available it serves every call from the local model.

2. **uv** for dependency management / execution (no manual venv activation):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. Dependencies (installs `ddgs`, `tavily-python`, `crawl4ai`, `mcp`,
   `pydantic`, `httpx`, `python-dotenv`) and the crawl4ai browser:
   ```bash
   cd ai-personal-finance-agent
   uv sync
   uv run python -m playwright install chromium   # for fetch_url
   cp .env.example .env                            # optional TAVILY_API_KEY
   ```

> **Local-only note.** This repo was captured on a machine where the gateway had
> **no cloud keys**, so the only available worker was Ollama. To pin the best
> local model without touching the shared gateway config, the agent honours an
> optional `AGENT_MODEL_OVERRIDE` env var that rides along as `ChatRequest.model`
> (it does **not** disable `auto_route`). On a machine with real provider keys,
> leave it unset and the gateway picks per-tier workers normally.

---

## Running the four queries

```bash
# clean state between independent attempts
rm -rf state/ sandbox/ usage.json

# captured on this machine (Ollama-only) with the local-model override:
export AGENT_MODEL_OVERRIDE=gemma4:e4b      # omit on a machine with cloud keys

uv run python agent6.py --query A     # emergency fund + SIP plan
uv run python agent6.py --query B     # home-loan EMI + USD→INR gift (live FX)
uv run python agent6.py --query C1    # records durable facts  (run 1)
uv run python agent6.py --query C2    # reads them back + computes (run 2)
uv run python agent6.py --query D     # live web research (EPF rate) + corpus

# or any free-form question:
uv run python agent6.py "I earn 90k, spend 55k, how much emergency fund do I need?"
```

---

## The four target queries — expected answers & iteration bounds

A query is considered passing only if it converges within **twice** its expected
iteration count.

| # | Query | Expected answer | Expected iters | 2× bound | Actual |
|---|---|---|---|---|---|
| **A** | Emergency fund + months to build + 10-yr SIP corpus | EF **₹4,20,000** (6× ₹70k); **6 months**; SIP ₹50k → **₹1,16,16,953.82** | 5 | 10 | **5** ✅ |
| **B** | Home-loan EMI + USD 5,000 gift in INR | EMI **₹17,994.52**; USD 5,000 → **₹4,75,007** (live) | 3 | 6 | **3** ✅ |
| **C1** | Remember risk appetite, retirement goal, age | 3 facts persisted to `state/memory.json` | 1 | 2 | **1** ✅ |
| **C2** | Required monthly SIP for the remembered goal @12% | **₹14,164.65 / month** (₹5 cr, 30 yrs) | 2 | 4 | **2** ✅ |
| **D** | Current EPF rate (FY 2024-25) + 25-yr corpus | EPF **8.25%**; ₹12,500/mo → **₹1,24,67,479.65** | 4 | 8 | **3** ✅ |

> FX (Query B) is a live rate from frankfurter.dev, so the INR figure moves with
> the market; the capture below used the rate on its run date.

---

## Durable memory (Query C)

Memory persists in `state/memory.json` and survives across separate process
runs. **Run 1 (`C1`)** has Perception extract `facts_to_remember`, which the
Memory layer writes to disk. **Run 2 (`C2`)** — a brand-new process — reads them
back through the Memory layer's recall and the Decision layer computes the answer
from them. The `state/` directory is git-ignored and wipeable between attempts:

```bash
rm -rf state/
```

`state/memory.json` after `C1` (each fact carries its provenance — source query, run id, timestamp):

```jsonc
{
  "facts": [
    {
      "key": "risk appetite", "value": "aggressive",
      "source_query": "Please remember the following about me ...",
      "run_id": "C1", "stored_at": "2026-05-30T05:33:03.902058+00:00"
    },
    {
      "key": "retirement goal", "value": "corpus of 50000000 by age 60",
      "source_query": "Please remember the following about me ...",
      "run_id": "C1", "stored_at": "2026-05-30T05:33:03.902058+00:00"
    },
    {
      "key": "current age", "value": "30",
      "source_query": "Please remember the following about me ...",
      "run_id": "C1", "stored_at": "2026-05-30T05:33:03.902058+00:00"
    }
  ]
}
```

---

## Perception & Decision prompts + Validation JSON (Proof of Perception)

### Perception system prompt (verbatim)
```
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
```

### Decision system prompt (verbatim)
```
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
    "- Be conservative; separate facts from assumptions; never guarantee returns."
```

### Validation JSON of PoP (Perception output for Query A, schema-validated `PerceptionOutput`)

The Perception layer returns this via `response_format=json_schema`; it is parsed
by `PerceptionOutput.model_validate(...)` — **no regex**:

```jsonc
{
  "request_types": ["Emergency Planning", "Goal Planning", "Investing", "Cash Flow"],
  "context": {
    "age": 30, "country": "India", "monthly_income": null, ...
  },
  "missing_information": ["Recommended size of the emergency fund (e.g., 3-6 months of expenses)", ...],
  "facts_to_remember": [],
  "needs_recall": false
}
```

For Query C1 the same contract carries the durable facts:

```jsonc
{
  "request_types": ["Retirement", "Risk Analysis"],
  "context": { "age": 30, "risk_appetite": "aggressive", "financial_goal": "corpus of 50000000 by age 60", ... },
  "facts_to_remember": [
    {"key": "risk appetite",   "value": "aggressive"},
    {"key": "retirement goal", "value": "corpus of 50000000 by age 60"},
    {"key": "current age",     "value": "30"}
  ],
  "needs_recall": false
}
```

---

## Captured terminal output (clean state, this machine)

### Query A — emergency fund + SIP corpus
```

==============================================================================
QUERY  (run_id=A)
==============================================================================
I'm 30 years old in India. I earn Rs 1,20,000 per month and my expenses are Rs 70,000 per month. I have Rs 1,50,000 in savings but no dedicated emergency fund. How big should my emergency fund be, how many months of my surplus will it take to build it, and how much can I then invest in a SIP each month? Assume the SIP earns 12% per year; project the corpus after 10 years.

==============================================================================
PERCEPTION  (auto_route=perception, schema-validated)
==============================================================================
{
  "request_types": [
    "Emergency Planning",
    "Goal Planning",
    "Investing",
    "Cash Flow"
  ],
  "context": {
    "age": 30,
    "monthly_income": null,
    "monthly_expenses": null,
    "existing_loans": null,
    "savings": null,
    "investments": null,
    "emergency_fund": null,
    "risk_appetite": null,
    "dependents": null,
    "time_horizon_years": null,
    "country": "India",
    "tax_regime": null,
    "financial_goal": null,
    "currency": null
  },
  "missing_information": [
    "The recommended number of months for the emergency fund (e.g., 6 months, 12 months) is needed to calculate the target corpus."
  ],
  "facts_to_remember": [],
  "needs_recall": false
}

==============================================================================
MEMORY  (durable store under state/memory.json, auto_route=memory)
==============================================================================
Durable store now holds: []

==============================================================================
TOOLS available from MCP server (stdio)
==============================================================================
web_search, fetch_url, get_time, currency_convert, finance_calc, read_file, list_dir, create_file, update_file, edit_file

==============================================================================
DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)
==============================================================================

-- iteration 1 [TINY->ollama:gemma4:e4b]
   thought: First, I need to calculate the monthly surplus to determine the monthly contribution for the emergency fund.
   tool: finance_calc({"operation": "surplus", "params": {"monthly_income": 120000, "monthly_expenses": 70000}})
         -> ok: {"operation": "surplus", "inputs": {"monthly_income": 120000, "monthly_expenses": 70000}, "monthly_surplus": 50000, "savings_rate_pct": 41.67, "formula": "surplus = income - expenses"}

-- iteration 2 [TINY->ollama:gemma4:e4b]
   thought: The surplus is calculated, so the next step is to determine the required emergency fund size.
   tool: finance_calc({"operation": "emergency_fund", "params": {"monthly_expenses": 70000, "months": 6}})
         -> ok: {"operation": "emergency_fund", "inputs": {"monthly_expenses": 70000, "months": 6}, "target_fund": 420000, "months_of_cover": 6, "formula": "target = monthly_expenses * months"}

-- iteration 3 [TINY->ollama:gemma4:e4b]
   thought: The user wants to know how long it takes to build the emergency fund and the resulting SIP amount.
   tool: finance_calc({"operation": "months_to_goal", "params": {"target": 420000, "current": 150000, "monthly_contribution": 50000}})
         -> ok: {"operation": "months_to_goal", "inputs": {"target": 420000, "current": 150000, "monthly_contribution": 50000}, "months_needed": 6, "remaining_to_save": 270000, "formula": "months = ceil(max(target - current, 0) / monthly_contribution)"}

-- iteration 4 [TINY->ollama:gemma4:e4b]
   thought: The next step is to calculate the future value of the SIP using the derived monthly surplus.
   tool: finance_calc({"operation": "sip_future_value", "params": {"monthly": 50000, "annual_rate_pct": 12, "years": 10}})
         -> ok: {"operation": "sip_future_value", "inputs": {"monthly": 50000, "annual_rate_pct": 12, "years": 10}, "future_value": 11616953.82, "total_invested": 6000000, "wealth_gain": 5616953.82, "months": 120, "formula": "FV = P * (((1+r)^n - 1)/r) * (1+r), monthly compounding, SIP at start of month"}

-- iteration 5 [TINY->ollama:gemma4:e4b]
   thought: All required calculations have been performed, so the next step is to finalize the answer.
   decision: finalize -> synthesize answer

==============================================================================
FINAL_RESPONSE  (iterations=5, tools=['finance_calc', 'finance_calc', 'finance_calc', 'finance_calc'])
==============================================================================
{
  "numeric_answer": "Rs 11,616,953.82",
  "summary": "Based on your current income and expenses, your monthly surplus is Rs 50,000. To establish a 6-month emergency fund (target: Rs 420,000), you need 6 months of dedicated saving. After achieving this, you can invest your full Rs 50,000 monthly surplus into a SIP, projecting a corpus of Rs 11,616,953.82 after 10 years.",
  "key_findings": [
    "Your current monthly surplus is Rs 50,000 (Rs 1,20,000 income - Rs 70,000 expenses).",
    "A recommended 6-month emergency fund target is Rs 420,000 (6 months * Rs 70,000 expenses).",
    "With your current savings rate, it will take 6 months to reach the Rs 420,000 emergency fund target.",
    "If you invest the full Rs 50,000 monthly surplus for 10 years at 12% CAGR, the projected corpus is Rs 11,616,953.82."
  ],
  "risks": [
    "Inflation Risk: The Rs 70,000 expense estimate may be outdated, and inflation could increase your required emergency fund size.",
    "Income Volatility: Any reduction in your Rs 1,20,000 monthly income will significantly delay the emergency fund build-up and SIP contributions.",
    "Opportunity Cost: Delaying the investment phase to build the emergency fund means missing out on early compounding returns."
  ],
  "recommended_actions": [
    "Prioritize building the emergency fund (Target: Rs 420,000) before aggressive investing to ensure financial stability.",
    "Automate the Rs 50,000 surplus transfer: Dedicate the first 6 months' savings entirely to the emergency fund.",
    "Review and optimize expenses: Identify areas to reduce the Rs 70,000 monthly expenditure to accelerate the savings timeline."
  ],
  "priority_order": [
    "Phase 1 (Immediate): Build Emergency Fund (Goal: Rs 420,000). Focus 100% of surplus here.",
    "Phase 2 (Short-Term): Maintain the fund and start systematic investment (SIP) once the target is hit.",
    "Phase 3 (Long-Term): Increase SIP contribution or review investment goals to maximize corpus growth."
  ],
  "short_term_plan": [
    "Allocate the next 6 months' surplus (Rs 50,000/month) directly into a liquid savings account for the emergency fund.",
    "Maintain a strict budget tracking system to ensure expenses do not exceed Rs 70,000.",
    "Upon reaching Rs 420,000, immediately transition the full Rs 50,000 monthly surplus into the SIP."
  ],
  "long_term_plan": [
    "Maintain the SIP contribution of Rs 50,000 monthly for the next 10 years to achieve the projected corpus of Rs 11,616,953.82.",
    "Review the investment portfolio annually to ensure the 12% return assumption remains realistic and diversified.",
    "Increase the SIP amount over time (e.g., by 5-10% annually) as your income grows to accelerate wealth creation."
  ],
  "assumptions": [
    "The current monthly expense of Rs 70,000 remains constant for the next 6 months.",
    "The monthly income of Rs 1,20,000 remains stable for the next 10 years.",
    "The SIP investment will consistently earn 12% CAGR over the 10-year period, compounded monthly."
  ],
  "follow_up_questions": [],
  "reasoning_types": [
    "Arithmetic",
    "Risk Analysis",
    "Forecasting",
    "Constraint Optimization"
  ]
}
```

### Query B — home-loan EMI + live USD→INR conversion
```

==============================================================================
QUERY  (run_id=B)
==============================================================================
I want to take a home loan of Rs 20,00,000 at 9% annual interest for 20 years. What will my monthly EMI be? Also, my uncle in the US wants to gift me USD 5,000 toward the down payment - how much is that in INR at today's exchange rate?

==============================================================================
PERCEPTION  (auto_route=perception, schema-validated)
==============================================================================
{
  "request_types": [
    "Debt",
    "Comparison"
  ],
  "context": {
    "age": null,
    "monthly_income": null,
    "monthly_expenses": null,
    "existing_loans": null,
    "savings": null,
    "investments": null,
    "emergency_fund": null,
    "risk_appetite": null,
    "dependents": null,
    "time_horizon_years": null,
    "country": null,
    "tax_regime": null,
    "financial_goal": null,
    "currency": null
  },
  "missing_information": [
    "Current exchange rate (USD to INR)"
  ],
  "facts_to_remember": [],
  "needs_recall": false
}

==============================================================================
MEMORY  (durable store under state/memory.json, auto_route=memory)
==============================================================================
Durable store now holds: []

==============================================================================
TOOLS available from MCP server (stdio)
==============================================================================
web_search, fetch_url, get_time, currency_convert, finance_calc, read_file, list_dir, create_file, update_file, edit_file

==============================================================================
DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)
==============================================================================

-- iteration 1 [TINY->ollama:gemma4:e4b]
   thought: The user asks for two calculations: an EMI and a currency conversion. I will start with the currency conversion.
   tool: currency_convert({"amount": 5000, "from_currency": "USD", "to_currency": "INR"})
         -> ok: {"amount": 5000.0, "from": "USD", "to": "INR", "rate": 95.0014, "converted": 475007, "date": "2026-05-29", "source": "frankfurter.dev"}

-- iteration 2 [TINY->ollama:gemma4:e4b]
   thought: The currency conversion is done, so I must now calculate the EMI using the finance_calc tool.
   tool: finance_calc({"operation": "emi", "params": {"principal": 2000000, "annual_rate_pct": 9, "years": 20}})
         -> ok: {"operation": "emi", "inputs": {"principal": 2000000, "annual_rate_pct": 9, "years": 20}, "emi": 17994.52, "total_payment": 4318684.59, "total_interest": 2318684.59, "months": 240, "formula": "EMI = P*r*(1+r)^n / ((1+r)^n - 1), r = annual%/12"}

-- iteration 3 [TINY->ollama:gemma4:e4b]
   thought: Both the EMI and the currency conversion have been calculated, so I can finalize the answer.
   decision: finalize -> synthesize answer

==============================================================================
FINAL_RESPONSE  (iterations=3, tools=['currency_convert', 'finance_calc'])
==============================================================================
{
  "numeric_answer": "The estimated monthly EMI for the home loan is Rs 17,994.52, and the USD 5,000 gift is equivalent to Rs 475,007.",
  "summary": "Based on the provided loan details and the current exchange rate, your estimated monthly EMI for a Rs 20,00,000 home loan over 20 years at 9% annual interest is **Rs 17,994.52**. Additionally, your uncle's gift of USD 5,000 is valued at **Rs 475,007** in Indian Rupees.",
  "key_findings": [
    "The calculated EMI for the Rs 20,00,000 loan is Rs 17,994.52, which is the fixed monthly payment required.",
    "The total repayment over 20 years (240 months) will be Rs 43,18,684.59.",
    "The total interest paid over the life of the loan is Rs 23,18,684.59, representing the cost of borrowing."
  ],
  "risks": [
    "Interest Rate Fluctuation Risk: If the actual interest rate increases above 9%, the EMI will rise, increasing the total cost.",
    "Inflation Risk: Over 20 years, inflation could significantly reduce the purchasing power of the EMI payments.",
    "Employment/Income Stability Risk: Maintaining consistent income for 20 years is critical to meeting the fixed EMI obligation."
  ],
  "recommended_actions": [
    "Explore Pre-Payment Options: Investigate options for making lump-sum payments or prepayments to reduce the principal faster and lower the total interest paid.",
    "Secure a Fixed Rate Loan: If possible, negotiate for a loan product that locks in the interest rate for a significant portion of the term.",
    "Budget for Contingency: Allocate a separate emergency fund equivalent to at least 6 months of the EMI (approx. Rs 107,967) to cover unexpected income gaps."
  ],
  "priority_order": [
    "Immediate: Confirm the final interest rate and processing fees with multiple lenders to ensure the 9% rate is accurate.",
    "High: Determine the optimal loan structure (e.g., fixed vs. floating rate) based on current market predictions.",
    "Medium: Calculate the impact of the down payment (Rs 475,007) on the required loan principal to reduce the EMI."
  ],
  "short_term_plan": [
    "Utilize the USD 5,000 gift (Rs 475,007) to reduce the initial loan principal, thereby lowering the required EMI.",
    "Establish a dedicated savings account for the down payment and associated closing costs.",
    "Create a detailed budget that incorporates the EMI (Rs 17,994.52) as a non-negotiable fixed expense."
  ],
  "long_term_plan": [
    "Aggressively pay down the principal using surplus income to minimize the total interest paid over the 20-year term.",
    "Build a robust investment portfolio separate from the home purchase to ensure financial resilience after the loan is paid off.",
    "Review refinancing options every 5-7 years to take advantage of potentially lower market interest rates."
  ],
  "assumptions": [
    "The annual interest rate of 9% remains constant and fixed for the entire 20-year loan term.",
    "The exchange rate of 95.0014 INR/USD remains stable until the funds are utilized.",
    "The user's income and employment status remain stable and sufficient to cover the EMI of Rs 17,994.52."
  ],
  "follow_up_questions": [],
  "reasoning_types": [
    "Arithmetic",
    "Risk Analysis",
    "Comparative Analysis",
    "Forecasting",
    "Constraint Optimization",
    "Scenario Planning"
  ]
}
```

### Query C1 — record durable facts (run 1)
```

==============================================================================
QUERY  (run_id=C1)
==============================================================================
Please remember the following about me for future sessions: my risk appetite is aggressive, my retirement goal is a corpus of Rs 5 crore by age 60, and I am 30 years old now.

==============================================================================
PERCEPTION  (auto_route=perception, schema-validated)
==============================================================================
{
  "request_types": [
    "Retirement",
    "Goal Planning"
  ],
  "context": {
    "age": 30,
    "monthly_income": null,
    "monthly_expenses": null,
    "existing_loans": null,
    "savings": null,
    "investments": null,
    "emergency_fund": null,
    "risk_appetite": "aggressive",
    "dependents": null,
    "time_horizon_years": null,
    "country": null,
    "tax_regime": null,
    "financial_goal": "corpus of 50000000 by age 60",
    "currency": null
  },
  "missing_information": [],
  "facts_to_remember": [
    {
      "key": "risk appetite",
      "value": "aggressive"
    },
    {
      "key": "retirement goal",
      "value": "corpus of 50000000 by age 60"
    },
    {
      "key": "current age",
      "value": "30"
    }
  ],
  "needs_recall": false
}

==============================================================================
MEMORY  (durable store under state/memory.json, auto_route=memory)
==============================================================================
Persisted 3 fact(s): risk appetite, retirement goal, current age
Durable store now holds: ['risk appetite', 'retirement goal', 'current age']
Recalled for this query:
  - risk appetite: aggressive
  - retirement goal: corpus of 50000000 by age 60
  - current age: 30

==============================================================================
TOOLS available from MCP server (stdio)
==============================================================================
web_search, fetch_url, get_time, currency_convert, finance_calc, read_file, list_dir, create_file, update_file, edit_file

==============================================================================
DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)
==============================================================================

-- iteration 1 [TINY->ollama:gemma4:e4b]
   thought: The user provided personal details, so I should acknowledge them and ask for the next piece of financial information.
   decision: finalize -> synthesize answer

==============================================================================
FINAL_RESPONSE  (iterations=1, tools=[])
==============================================================================
{
  "numeric_answer": "Rs 5,00,00,000",
  "summary": "Your retirement goal is to accumulate a corpus of Rs 5 crore by age 60. Given your current age of 30 and aggressive risk appetite, we have established a clear target. To proceed with a comprehensive financial plan, please provide your current financial details, such as your current savings, investments, and annual income.",
  "key_findings": [
    "Retirement Goal: Rs 5,00,00,000 (5 Crore) by age 60.",
    "Time Horizon: 30 years (from age 30 to age 60).",
    "Risk Profile: Aggressive, indicating a willingness to take higher risks for potentially higher returns."
  ],
  "risks": [
    "Inflation Risk: The real value of Rs 5 crore in 30 years will be significantly lower due to inflation.",
    "Market Volatility Risk: Aggressive investments are susceptible to significant short-term market downturns.",
    "Goal Drift Risk: Without regular contributions and monitoring, the goal may drift due to lifestyle changes or income fluctuations."
  ],
  "recommended_actions": [
    "Determine Current Financial Snapshot: Provide details on existing savings, investments, and liabilities.",
    "Calculate Required Rate of Return: Based on the goal and time horizon, we must calculate the necessary CAGR.",
    "Establish Emergency Fund: Ensure a liquid emergency fund (6-12 months of expenses) is separate from long-term investments."
  ],
  "priority_order": [
    "1. Current Financial Assessment (Income, Expenses, Assets): This is the foundational step.",
    "2. Determine Required Savings Rate: Calculate the monthly/annual contribution needed to hit the target.",
    "3. Portfolio Allocation Strategy: Design an aggressive, diversified portfolio matching the 30-year timeline."
  ],
  "short_term_plan": [
    "Gather Income Data: Provide details on current salary, bonuses, and other regular income sources.",
    "Gather Expense Data: Provide a detailed breakdown of monthly expenditures (housing, food, debt, etc.).",
    "Review Existing Investments: List all current investments (mutual funds, stocks, real estate) and their current values."
  ],
  "long_term_plan": [
    "Optimize Asset Allocation: Structure the portfolio heavily towards growth assets (equities) given the aggressive risk profile.",
    "Implement Systematic Investment Plan (SIP): Commit to consistent, disciplined contributions over the next 30 years.",
    "Review and Adjust: Schedule annual reviews to recalibrate the plan based on market performance and life changes."
  ],
  "assumptions": [
    "Consistent Contributions: It is assumed that the user will maintain consistent, increasing contributions over the next 30 years.",
    "Inflation Rate: A conservative average inflation rate of 6% has been assumed for future planning.",
    "Investment Returns: A projected average annual return (CAGR) will be calculated based on the aggressive risk profile and time horizon."
  ],
  "follow_up_questions": [],
  "reasoning_types": [
    "Constraint Optimization",
    "Forecasting",
    "Comparative Analysis"
  ]
}
```

### Query C2 — read durable facts back + compute (run 2)
```

==============================================================================
QUERY  (run_id=C2)
==============================================================================
Based on what you already know about me, how much do I need to invest every month to reach my retirement goal, assuming my investments earn 12% per year? Show the required monthly SIP.

==============================================================================
PERCEPTION  (auto_route=perception, schema-validated)
==============================================================================
{
  "request_types": [
    "Retirement",
    "Investing",
    "Goal Planning"
  ],
  "context": {
    "age": null,
    "monthly_income": null,
    "monthly_expenses": null,
    "existing_loans": null,
    "savings": null,
    "investments": null,
    "emergency_fund": null,
    "risk_appetite": null,
    "dependents": null,
    "time_horizon_years": null,
    "country": null,
    "tax_regime": null,
    "financial_goal": null,
    "currency": null
  },
  "missing_information": [
    "Current age",
    "Target retirement age",
    "Current savings/corpus",
    "Target retirement corpus"
  ],
  "facts_to_remember": [],
  "needs_recall": true
}

==============================================================================
MEMORY  (durable store under state/memory.json, auto_route=memory)
==============================================================================
Durable store now holds: ['risk appetite', 'retirement goal', 'current age']
Recalled for this query:
  - risk appetite: aggressive
  - retirement goal: corpus of 50000000 by age 60
  - current age: 30

==============================================================================
TOOLS available from MCP server (stdio)
==============================================================================
web_search, fetch_url, get_time, currency_convert, finance_calc, read_file, list_dir, create_file, update_file, edit_file

==============================================================================
DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)
==============================================================================

-- iteration 1 [TINY->ollama:gemma4:e4b]
   thought: The user wants to calculate the required monthly SIP for retirement, given the goal and assumed rate of return.
   tool: finance_calc({"operation": "required_sip", "params": {"target": 50000000, "annual_rate_pct": 12, "years": 30.0}})
         -> ok: {"operation": "required_sip", "inputs": {"target": 50000000, "annual_rate_pct": 12, "years": 30.0}, "required_monthly_sip": 14164.65, "target": 50000000, "months": 360, "formula": "P = FV / ((((1+r)^n - 1)/r) * (1+r))"}

-- iteration 2 [TINY->ollama:gemma4:e4b]
   thought: The required SIP has been calculated, and all necessary information is available to answer the user.
   decision: finalize -> synthesize answer

==============================================================================
FINAL_RESPONSE  (iterations=2, tools=['finance_calc'])
==============================================================================
{
  "numeric_answer": "INR 14,164.65",
  "summary": "To reach your retirement goal of INR 50,000,000 in 30 years, assuming a consistent 12% annual return, you need to invest a Systematic Investment Plan (SIP) of **INR 14,164.65** every month.",
  "key_findings": [
    "The required monthly investment (SIP) is INR 14,164.65.",
    "Your target corpus is INR 50,000,000.",
    "The calculation assumes a 30-year time horizon and a 12% annual rate of return."
  ],
  "risks": [
    "**Market Volatility Risk:** The 12% annual return is an assumption. Actual market returns may be lower, requiring a higher SIP contribution.",
    "**Inflation Risk:** The INR 50,000,000 target is in today's money. Future inflation could necessitate a significantly higher corpus.",
    "**Behavioral Risk:** Maintaining consistent monthly investments (SIP) over 30 years requires discipline and resistance to market downturns."
  ],
  "recommended_actions": [
    "**Automate Investments:** Set up an automatic monthly transfer of INR 14,164.65 to ensure consistency and avoid lapses.",
    "**Review Annually:** Re-evaluate your required SIP every 3-5 years to adjust for changes in income, inflation, or market performance.",
    "**Increase Contribution:** If your income increases, consider increasing the SIP amount beyond the calculated minimum to build a larger safety buffer."
  ],
  "priority_order": [
    "**High Priority:** Immediately allocate INR 14,164.65 monthly to the investment vehicle.",
    "**Medium Priority:** Review and optimize your current investment portfolio allocation to align with an aggressive risk profile.",
    "**Low Priority:** Focus on increasing your overall savings rate, as this provides the most control over the final corpus."
  ],
  "short_term_plan": [
    "Establish the SIP of INR 14,164.65 within the next 30 days.",
    "Allocate the funds into diversified equity mutual funds (given the aggressive risk appetite).",
    "Build an emergency fund separate from retirement savings to cover 6-12 months of expenses."
  ],
  "long_term_plan": [
    "Maintain the aggressive investment strategy for the full 30-year period.",
    "Implement a systematic de-risking strategy (shifting from equity to debt) starting 5-10 years before retirement.",
    "Periodically increase the SIP amount (step-up SIP) by at least the rate of inflation (e.g., 5-7%) every few years."
  ],
  "assumptions": [
    "The investment returns will consistently average 12% per annum over 30 years.",
    "The required SIP of INR 14,164.65 remains constant in real terms (i.e., inflation is not factored into the required contribution).",
    "The user's current income and contribution capacity remain stable enough to sustain the required monthly investment."
  ],
  "follow_up_questions": [],
  "reasoning_types": [
    "Forecasting",
    "Risk Analysis",
    "Constraint Optimization"
  ]
}
```

### Query D — live web research (EPF rate) + corpus projection
```

==============================================================================
QUERY  (run_id=D)
==============================================================================
What is the current EPF (Employees' Provident Fund) interest rate in India for FY 2024-25? Using that rate, if I contribute Rs 12,500 per month for 25 years, what retirement corpus would I accumulate?

==============================================================================
PERCEPTION  (auto_route=perception, schema-validated)
==============================================================================
{
  "request_types": [
    "Retirement",
    "Forecasting"
  ],
  "context": {
    "age": null,
    "monthly_income": null,
    "monthly_expenses": null,
    "existing_loans": null,
    "savings": null,
    "investments": null,
    "emergency_fund": null,
    "risk_appetite": null,
    "dependents": null,
    "time_horizon_years": null,
    "country": null,
    "tax_regime": null,
    "financial_goal": null,
    "currency": null
  },
  "missing_information": [
    "The current EPF interest rate for FY 2024-25 (This is a factual query, not a missing user input, but is required for the calculation.)"
  ],
  "facts_to_remember": [],
  "needs_recall": false
}

==============================================================================
MEMORY  (durable store under state/memory.json, auto_route=memory)
==============================================================================
Durable store now holds: []

==============================================================================
TOOLS available from MCP server (stdio)
==============================================================================
web_search, fetch_url, get_time, currency_convert, finance_calc, read_file, list_dir, create_file, update_file, edit_file

==============================================================================
DECISION + ACTION loop  (decide auto_route=decision, dispatch via MCP)
==============================================================================

-- iteration 1 [TINY->ollama:gemma4:e4b]
   thought: The first step is to find the current EPF interest rate for FY 2024-25 using web search.
   tool: web_search({"query": "EPF interest rate India FY 2024-25", "max_results": 3})
         -> ok: [{"title": "EPFO retains 8.25% interest rate on employees' provident fund...", "url": "https://www.zeebiz.com/india/news-epfo-latest-interest-rate-retirement-fund-body-retains-interest-rate-of-825-per-cent-for-2024-and-2025-employees-provident-fund-organisation-news-update-349150", "snippet": "In March 2020, EPFO had lowered the interest rate on provident fund deposits to a seven-year low of 8.5 p ...[truncated 734 chars]

-- iteration 2 [TINY->ollama:gemma4:e4b]
   thought: The web search provided the EPF interest rate, which is 8.25% for FY 2024-25. Now I must use the finance_calc tool to find the future value.
   tool: finance_calc({"operation": "sip_future_value", "params": {"monthly": 12500, "annual_rate_pct": 8.25, "years": 25}})
         -> ok: {"operation": "sip_future_value", "inputs": {"monthly": 12500, "annual_rate_pct": 8.25, "years": 25}, "future_value": 12467479.65, "total_invested": 3750000, "wealth_gain": 8717479.65, "months": 300, "formula": "FV = P * (((1+r)^n - 1)/r) * (1+r), monthly compounding, SIP at start of month"}

-- iteration 3 [TINY->ollama:gemma4:e4b]
   thought: The EPF rate and the final corpus calculation have been completed, so I can finalize the answer.
   decision: finalize -> synthesize answer

==============================================================================
FINAL_RESPONSE  (iterations=3, tools=['web_search', 'finance_calc'])
==============================================================================
{
  "numeric_answer": "Rs 1,24,67,479.65",
  "summary": "Based on the current reported EPF interest rate of 8.25% for FY 2024-25, contributing Rs 12,500 per month over 25 years is projected to accumulate a retirement corpus of **Rs 1,24,67,479.65**.",
  "key_findings": [
    "The current reported EPF interest rate for FY 2024-25 is 8.25%, based on available market information.",
    "A consistent monthly contribution of Rs 12,500 over 25 years is projected to yield a total corpus of Rs 1,24,67,479.65.",
    "The total wealth gain from compounding interest is substantial, amounting to Rs 87,17,479.65 over the 25-year period."
  ],
  "risks": [
    "Interest Rate Fluctuation: The 8.25% rate is an estimate and is subject to change by the EPFO, which could significantly alter the final corpus.",
    "Inflation Risk: Over 25 years, inflation will erode the purchasing power of the accumulated corpus, requiring a higher withdrawal rate or larger corpus.",
    "Contribution Consistency: The calculation assumes uninterrupted contributions of Rs 12,500 monthly; any missed payments will reduce the final corpus."
  ],
  "recommended_actions": [
    "Review Annual Rate: Monitor official EPFO announcements for the final, confirmed interest rate for the relevant financial year.",
    "Increase Contribution: Consider increasing the monthly contribution amount (SIP) to accelerate corpus growth and build a larger safety net.",
    "Diversify Investments: Supplement EPF savings with investments in other asset classes (e.g., mutual funds, real estate) to mitigate single-source risk."
  ],
  "priority_order": [
    "Confirm Rate: Prioritize tracking the official EPFO announcement for the final interest rate.",
    "Optimize Contribution: Determine the maximum sustainable monthly contribution amount based on current income.",
    "Build Emergency Fund: Ensure a separate, liquid emergency fund is maintained, separate from long-term retirement savings."
  ],
  "short_term_plan": [
    "Establish SIP: Immediately set up an automated Systematic Investment Plan (SIP) for Rs 12,500 monthly contribution.",
    "Budget Review: Conduct a detailed budget review to identify areas where monthly savings can be increased.",
    "Goal Setting: Define specific short-term financial goals (e.g., down payment, car purchase) to maintain financial discipline."
  ],
  "long_term_plan": [
    "Retirement Corpus Goal: Set a target corpus that accounts for desired lifestyle and inflation (e.g., Rs 3-4 Crore).",
    "Review Frequency: Plan to review the entire retirement strategy every 3-5 years to adjust for life changes and market conditions.",
    "Tax Planning: Consult a financial advisor to optimize tax-saving investments (e.g., NPS, ELSS) alongside EPF contributions."
  ],
  "assumptions": [
    "Constant Rate: The calculation assumes a fixed annual interest rate of 8.25% for the entire 25-year period.",
    "Consistent Contribution: It assumes the monthly contribution of Rs 12,500 remains constant and is made at the start of each month.",
    "Taxation: The calculation does not account for potential changes in tax laws or withdrawal tax implications upon retirement."
  ],
  "follow_up_questions": [],
  "reasoning_types": [
    "Forecasting",
    "Risk Analysis",
    "Comparative Analysis"
  ]
}
```

---

## Demo video

▶️ **YouTube:** _add link here_ — end-to-end runs of all four queries.

---

## Design guarantees (assignment checklist)

- ✅ Four modules with clear separation: `memory.py`, `perception.py`, `decision.py`, `action.py`, wired by `agent6.py`.
- ✅ `schemas.py` holds all Pydantic v2 models; every layer boundary is typed (`extra="forbid"`).
- ✅ MCP server from earlier sessions, extended with `finance_calc`; stdio transport; dispatch via MCP `call_tool` (not reimplemented).
- ✅ Every LLM call goes through LLM Gateway V3 (`gateway.py`); no provider SDK imported.
- ✅ No regex on LLM output — structured output (`response_format`) is schema-validated by the gateway.
- ✅ Durable memory under `state/`, git-ignored and wipeable; Query C proves cross-run persistence.
- ✅ No third-party agentic framework (no LangChain / LangGraph / CrewAI).
- ✅ uv for dependencies and execution; no manual virtualenv activation.
- ✅ All four queries converge within 2× their expected iteration count.
