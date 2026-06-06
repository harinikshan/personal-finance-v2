"""
MCP server for the AI Personal Finance Agent (EAGV3 Session 6).

Ten tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert, finance_calc,
    read_file, list_dir, create_file, update_file, edit_file

web_search:   Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:    crawl4ai only — clean markdown via headless Chromium.
finance_calc: deterministic personal-finance math (EMI, SIP, CAGR, corpus,
              inflation, emergency fund). The decision layer calls this instead
              of doing arithmetic in the LLM, so answers are reproducible.

Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result

# Keep the stdio JSON-RPC stream clean and the captured demo output readable:
# FastMCP logs every request at INFO to stderr otherwise.
logging.getLogger("mcp").setLevel(logging.WARNING)

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("eagv3-s6-finance-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("auto", "html", "lite"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


async def _crawl4ai_fetch(url: str) -> dict:
    from crawl4ai import AsyncWebCrawler

    # crawl4ai uses Rich which writes via its own captured stdout reference, so
    # contextlib.redirect_stdout doesn't catch it. Redirect at the file-descriptor
    # level — crawl4ai's banner / [FETCH] / [SCRAPE] markers would otherwise
    # corrupt the MCP stdio JSON-RPC stream.
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            r = await crawler.arun(url=url)
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    # r.markdown is a str subclass (StringCompatibleMarkdown) that Pydantic
    # serializes as {} because its real field is private. Pull the raw string
    # out and force a plain str so FastMCP serializes correctly.
    md = r.markdown
    raw = (
        getattr(md, "raw_markdown", None)
        or getattr(md, "fit_markdown", None)
        or md
        or r.cleaned_html
        or r.html
        or ""
    )
    text = str(raw)
    return {
        "status": int(getattr(r, "status_code", None) or 200),
        "content_type": "text/markdown",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("current EPF interest rate India 2024-25", 3)."""
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


@mcp.tool()
async def fetch_url(url: str, timeout: int = 20) -> dict:
    """Fetch clean markdown from a URL via crawl4ai (headless Chromium). Example: fetch_url("https://example.com")."""
    return await _crawl4ai_fetch(url)


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev (live rates). Example: currency_convert(5000, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    converted = data["rates"][t]
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


# --------------------------------------------------------------------------- #
# finance_calc — deterministic personal-finance math (new in Session 6)
# --------------------------------------------------------------------------- #
def _emi(principal: float, annual_rate_pct: float, years: float) -> dict:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        emi = principal / n
    else:
        emi = principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
    total = emi * n
    return {
        "emi": round(emi, 2),
        "total_payment": round(total, 2),
        "total_interest": round(total - principal, 2),
        "months": round(n),
        "formula": "EMI = P*r*(1+r)^n / ((1+r)^n - 1), r = annual%/12",
    }


def _sip_future_value(monthly: float, annual_rate_pct: float, years: float) -> dict:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        fv = monthly * n
    else:
        fv = monthly * (((1 + r) ** n - 1) / r) * (1 + r)
    invested = monthly * n
    return {
        "future_value": round(fv, 2),
        "total_invested": round(invested, 2),
        "wealth_gain": round(fv - invested, 2),
        "months": round(n),
        "formula": "FV = P * (((1+r)^n - 1)/r) * (1+r), monthly compounding, SIP at start of month",
    }


def _required_sip(target: float, annual_rate_pct: float, years: float) -> dict:
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        monthly = target / n
    else:
        monthly = target / ((((1 + r) ** n - 1) / r) * (1 + r))
    return {
        "required_monthly_sip": round(monthly, 2),
        "target": target,
        "months": round(n),
        "formula": "P = FV / ((((1+r)^n - 1)/r) * (1+r))",
    }


def _cagr(begin_value: float, end_value: float, years: float) -> dict:
    if begin_value <= 0 or years <= 0:
        raise ValueError("begin_value and years must be positive")
    cagr = (end_value / begin_value) ** (1 / years) - 1
    return {"cagr_pct": round(cagr * 100, 4), "formula": "CAGR = (end/begin)^(1/years) - 1"}


def _compound(principal: float, annual_rate_pct: float, years: float,
              compounds_per_year: float = 1) -> dict:
    r = annual_rate_pct / 100 / compounds_per_year
    n = compounds_per_year * years
    fv = principal * (1 + r) ** n
    return {
        "future_value": round(fv, 2),
        "interest": round(fv - principal, 2),
        "formula": "FV = P*(1 + annual%/m)^(m*years)",
    }


def _inflation_adjust(amount: float, inflation_pct: float, years: float,
                      direction: str = "future") -> dict:
    factor = (1 + inflation_pct / 100) ** years
    if direction == "present":
        value = amount / factor
        note = "present value of a future amount"
    else:
        value = amount * factor
        note = "future cost of today's amount after inflation"
    return {"value": round(value, 2), "factor": round(factor, 6), "note": note}


def _emergency_fund(monthly_expenses: float, months: float = 6) -> dict:
    return {
        "target_fund": round(monthly_expenses * months, 2),
        "months_of_cover": months,
        "formula": "target = monthly_expenses * months",
    }


def _months_to_goal(target: float, current: float, monthly_contribution: float) -> dict:
    remaining = max(target - current, 0.0)
    if monthly_contribution <= 0:
        raise ValueError("monthly_contribution must be positive")
    months = math.ceil(remaining / monthly_contribution) if remaining > 0 else 0
    return {
        "months_needed": months,
        "remaining_to_save": round(remaining, 2),
        "formula": "months = ceil(max(target - current, 0) / monthly_contribution)",
    }


def _surplus(monthly_income: float, monthly_expenses: float) -> dict:
    s = monthly_income - monthly_expenses
    rate = (s / monthly_income * 100) if monthly_income else 0.0
    return {
        "monthly_surplus": round(s, 2),
        "savings_rate_pct": round(rate, 2),
        "formula": "surplus = income - expenses",
    }


_FINANCE_OPS = {
    "emi": (_emi, ["principal", "annual_rate_pct", "years"]),
    "sip_future_value": (_sip_future_value, ["monthly", "annual_rate_pct", "years"]),
    "required_sip": (_required_sip, ["target", "annual_rate_pct", "years"]),
    "cagr": (_cagr, ["begin_value", "end_value", "years"]),
    "compound": (_compound, ["principal", "annual_rate_pct", "years"]),
    "inflation_adjust": (_inflation_adjust, ["amount", "inflation_pct", "years"]),
    "emergency_fund": (_emergency_fund, ["monthly_expenses"]),
    "surplus": (_surplus, ["monthly_income", "monthly_expenses"]),
    "months_to_goal": (_months_to_goal, ["target", "current", "monthly_contribution"]),
}


@mcp.tool()
def finance_calc(operation: str, params: dict) -> dict:
    """Deterministic personal-finance math. Always use this instead of doing arithmetic yourself.

    operation must be one of:
      - emi(principal, annual_rate_pct, years)                  -> monthly loan EMI
      - sip_future_value(monthly, annual_rate_pct, years)       -> corpus from a monthly SIP
      - required_sip(target, annual_rate_pct, years)            -> monthly SIP to reach a target
      - cagr(begin_value, end_value, years)                     -> annualised growth rate %
      - compound(principal, annual_rate_pct, years[, compounds_per_year]) -> lump-sum future value
      - inflation_adjust(amount, inflation_pct, years[, direction]) -> direction "future"|"present"
      - emergency_fund(monthly_expenses[, months])              -> target emergency corpus (default 6 months)
      - surplus(monthly_income, monthly_expenses)               -> monthly surplus + savings rate
      - months_to_goal(target, current, monthly_contribution)   -> months to save from current to target

    Pass numeric arguments inside `params`. Rates are PERCENT per year (e.g. 9 for 9%).
    Example: finance_calc("emi", {"principal": 2000000, "annual_rate_pct": 9, "years": 20}).
    """
    op = operation.strip().lower()
    if op not in _FINANCE_OPS:
        raise ValueError(f"unknown operation '{operation}'. valid: {sorted(_FINANCE_OPS)}")
    fn, required = _FINANCE_OPS[op]
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError(f"operation '{op}' missing params: {missing}")
    clean = {k: v for k, v in params.items()}
    result = fn(**clean)
    if any(isinstance(v, float) and (math.isnan(v) or math.isinf(v)) for v in result.values()):
        raise ValueError("calculation produced a non-finite result; check inputs")
    return {"operation": op, "inputs": clean, **result}


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> list[dict]:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    p = _safe(path)
    out = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        out.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
    return out


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
