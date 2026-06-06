# 🎥 Demo Run Guide — AI Personal Finance Agent (EAGV3 Session 6)

A 4-scenario screen-recording script: **Perception → Memory → Decision → Action**,
every LLM call through **LLM Gateway V3**, every tool call over **MCP stdio**,
every layer boundary a typed **Pydantic v2** contract (no regex on model output).

---

## 0. Pre-flight (do this once, before you hit record)

Open **3 terminals**.

**Terminal 1 — LLM Gateway V3** (the substrate every LLM call goes through):
```bash
cd /Users/cloudtrade/Desktop/llm_gatewayV3
./run.sh
# wait for "Application startup complete", then sanity check:
curl -s http://localhost:8101/v1/status | python3 -m json.tool | head
```

**Terminal 2 — Ollama** (local fallback worker; keep it alive):
```bash
ollama serve            # leave running; gemma4:e4b is already pulled
```

**Terminal 3 — the agent** (this is the one you record):
```bash
cd /Users/cloudtrade/assigment_6
# pin a reliable 70B worker with large free-tier quota
export AGENT_PROVIDER=nvidia
# fail fast instead of hanging on a slow provider
export LLM_GATEWAY_TIMEOUT=120
# clean slate so the C1->C2 memory demo is honest (run this RIGHT before recording)
rm -f state/memory.json
cat state/memory.json 2>/dev/null || echo "memory cleared ✓"
```

> ⚠️ Keep each `export` and its `#comment` on **separate lines** — your shell parses
> an inline `#` as an argument (that's the `not an identifier` error). And re-run the
> `rm` immediately before recording, or scenarios A/B/D will show stale "Recalled"
> facts and the C1→C2 story won't be clean.

> **Optional B-roll:** open the live gateway dashboard at **http://localhost:8101/**
> in a browser to show provider routing / call log while the agent runs.

---

## 1. The four demo scenarios

Run each command in **Terminal 3**. Narrate what each layer does as the trace scrolls.

### 🅐 Scenario 1 — Multi-step planning + deterministic math
```bash
uv run python agent6.py --query A
```
**Query:** *"I'm 30, earn ₹1,20,000/mo, spend ₹70,000/mo, have ₹1,50,000 saved…
emergency fund? months to build? monthly SIP? project 10-yr corpus."*

**Point out on screen:**
- **PERCEPTION** extracts a schema-valid `FinancialContext` (income/expenses/savings).
- **DECISION→ACTION loop** plans **one typed `NextAction` at a time** and calls the
  `finance_calc` MCP tool 6× — *no arithmetic is done by the LLM*.
- **Expected:** surplus ₹50,000 → emergency fund **₹4,20,000** → SIP **₹50k/mo → ₹1.16 crore** in 10 yr.

### 🅑 Scenario 2 — Live external data (FX) + financial formula
```bash
  uv run python agent6.py --query B
```
**Query:** *"₹20,00,000 home loan @ 9% for 20 yr — monthly EMI? Also convert a USD 5,000 gift to INR at today's rate."*

**Point out:**
- `finance_calc` computes the **EMI** deterministically.
- `currency_convert` fetches a **live USD→INR rate** (real tool call, not a guess).
- **Expected:** EMI **₹17,994.52/mo**; USD 5,000 ≈ **₹4,74,749**.

### 🅒 Scenario 3 — Durable memory ACROSS SEPARATE RUNS (the headline feature)
Run these as **two separate processes** — that's the whole point.
```bash
# Run 1 — WRITE: the user asks the agent to remember facts
uv run python agent6.py --query C1
cat state/memory.json         # show the facts persisted to disk

# Run 2 — READ BACK: a brand-new process recalls them and computes
uv run python agent6.py --query C2
```
**Point out:**
- **C1** persists `risk_appetite`, `retirement_goal (₹5 cr → 50000000)`, `current_age (30)`,
  `retirement_age (60)` to `state/memory.json`.
- **C2** is a *fresh process* — it **recalls** those facts (`needs_recall=true`),
  derives the **30-year** horizon (60−30) from memory, and computes the SIP.
- **Expected:** required SIP **₹14,164.65/mo** to reach ₹5 crore.
- 💡 *Show `state/memory.json` between the two runs to prove it's real on-disk durability.*

### 🅓 Scenario 4 — Live web research + computation
```bash
uv run python agent6.py --query D
```
**Query:** *"What's the current EPF interest rate for FY 2024-25? Using it, project the corpus for ₹12,500/mo over 25 years."*

**Point out:**
- `web_search` finds the rate from the **official EPFO source** (live web).
- `finance_calc` projects the corpus at that rate.
- **Expected:** EPF rate **8.25%** → corpus ≈ **₹1.25 crore**.

---

## 2. One-shot version (if you want a single continuous take)
```bash
cd /Users/cloudtrade/assigment_6
export AGENT_PROVIDER=nvidia LLM_GATEWAY_TIMEOUT=120
rm -f state/memory.json
for q in A B C1 C2 D; do
  echo "================  QUERY $q  ================"
  uv run python agent6.py --query $q
  sleep 15        # let per-minute rate limits breathe between queries
done
```

## 3. Talking points to land in the voiceover
- **Four cognitive layers**, each a separate module with a **typed contract** — no free-form dicts.
- **No provider SDK** is imported anywhere — *every* LLM call goes through LLM Gateway V3.
- **No regex on model output** — the gateway constrains workers to a JSON schema.
- **Tool dispatch is real MCP** (`ClientSession.call_tool`), not a reimplementation.
- **All arithmetic is delegated** to a deterministic calculator tool — the LLM only *plans*.
- **Memory survives across processes** — proven live by C1 (write) → C2 (read in a new run).
