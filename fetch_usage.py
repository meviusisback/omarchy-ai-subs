#!/usr/bin/env python3
"""Hermes provider usage/balance fetcher for the Omarchy bar plugin.

Replicates the fetch half of meviusisback/usage-stats (Hermes Desktop plugin):
reads provider API keys from the Hermes profile .env, queries each vendor's
usage/balance endpoint in parallel, and prints a JSON document the QML panel
renders. Stdlib-only (urllib) so it runs under the system python3 with no deps.

Provider mapping (display -> vendor -> metric):
  OC OpenCode Go      % used (rolling 5h / weekly / monthly)
  OR OpenRouter       USD credits remaining
  DS DeepSeek         USD balance
  KI Kimi/Moonshot    CNY balance
  NV NovitaAI         USD balance
  Z  ZAI/Zhipu        CNY balance
  AB Alibaba/DashScope CNY usage
  AR Arcee AI         USD balance
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------------------------------------------------- #
# Endpoints + transport
# --------------------------------------------------------------------------- #
USAGE_API_URL = "https://opencode.ai/zen/go/v1/usage"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
KIMI_BALANCE_URL = "https://api.moonshot.cn/v1/users/me/balance"
NOVITA_BALANCE_URL = "https://api.novita.ai/v3/account/balance"
ZAI_BALANCE_URL = "https://open.bigmodel.cn/api/paas/v4/user/balance"
ALIBABA_BILLING_URL = "https://dashscope.aliyuncs.com/api/v1/services/billing/usage"
ARCEE_BALANCE_URL = "https://api.arcee.ai/v2/user/balance"

TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 4096
USER_AGENT = "Mozilla/5.0 (Hermes-Agent; usage-stats)"
WINDOWS = [
    {"id": "rolling", "label": "5h"},
    {"id": "weekly", "label": "W"},
    {"id": "monthly", "label": "M"},
]


def _read_key(env_name: str) -> str | None:
    value = os.environ.get(env_name, "").strip()
    return value or None


def _request_json(url: str, api_key: str):
    # OpenCode's edge rejects the default Python-urllib User-Agent with 403,
    # so we send a browser-like one everywhere.
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response-too-large")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _request_usage(api_key: str):
    return _request_json(USAGE_API_URL, api_key)


def _normalize_usage(body):
    raw_usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(raw_usage, dict):
        return None
    normalized = {}
    for window in WINDOWS:
        window_id = window["id"]
        raw = raw_usage.get(window_id)
        if not isinstance(raw, dict):
            normalized[window_id] = None
            continue
        try:
            percent = round(float(raw["percent"]), 1) if raw.get("percent") is not None else None
        except (TypeError, ValueError):
            percent = None
        normalized[window_id] = {
            "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
            "percent": percent,
            "resetsAt": raw.get("resetsAt") if isinstance(raw.get("resetsAt"), str) else None,
        }
    return normalized


def _pct(value):
    return f"{round(value)}%" if value is not None else "—"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_balance(body, field_names, *, unwrap_data: bool = False):
    inner = body.get("data") if unwrap_data and isinstance(body.get("data"), dict) else body
    for key in field_names:
        if key in inner:
            return _safe_float(inner[key])
    return None


# --------------------------------------------------------------------------- #
# Provider fetchers
# --------------------------------------------------------------------------- #
def _fetch_opencode(api_key: str):
    body = _request_usage(api_key)
    norm = _normalize_usage(body)
    if norm is None:
        return {"error": "unexpected-response"}
    windows = [
        {
            "id": window["id"],
            "label": window["label"],
            "percent": (norm.get(window["id"]) or {}).get("percent"),
            "resetsAt": (norm.get(window["id"]) or {}).get("resetsAt"),
        }
        for window in WINDOWS
    ]
    rolling = norm.get("rolling") or {}
    weekly = norm.get("weekly") or {}
    monthly = norm.get("monthly") or {}
    rolling_pct = rolling.get("percent")
    weekly_pct = weekly.get("percent")
    monthly_pct = monthly.get("percent")
    headline = rolling_pct if rolling_pct is not None else (weekly_pct if weekly_pct is not None else monthly_pct)
    detail = f"rolling 5h {_pct(rolling_pct)} · weekly {_pct(weekly_pct)} · monthly {_pct(monthly_pct)}"
    return {
        "kind": "percent",
        "label": _pct(headline),
        "value": headline,
        "detail": detail,
        "windows": windows,
    }


def _fetch_openrouter(api_key: str):
    body = _request_json(OPENROUTER_CREDITS_URL, api_key)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("total_credits") is None or data.get("total_usage") is None:
        return {"error": "unexpected-response"}
    total = _safe_float(data["total_credits"])
    used = _safe_float(data["total_usage"])
    remaining = max(0.0, total - used)
    pct_used = (used / total * 100.0) if total > 0 else 0.0
    return {
        "kind": "balance",
        "label": f"${remaining:,.2f}",
        "value": round(remaining, 2),
        "used": round(used, 2),
        "total": round(total, 2),
        "ratio": round(pct_used / 100.0, 4) if total > 0 else None,
        "detail": f"${remaining:,.2f} left of ${total:,.2f} ({pct_used:.0f}% used)",
    }


def _fetch_deepseek(api_key: str):
    body = _request_json(DEEPSEEK_BALANCE_URL, api_key)
    infos = body.get("balance_infos") if isinstance(body, dict) else None
    if not isinstance(infos, list) or not infos:
        return {"error": "unexpected-response"}
    total = 0.0
    currency = "USD"
    for info in infos:
        if not isinstance(info, dict):
            continue
        currency = info.get("currency") or currency
        total += _safe_float(info.get("total_balance"))
    value = round(total, 2)
    return {
        "kind": "balance",
        "label": f"{value:,.2f} {currency}",
        "value": value,
        "currency": currency,
        "detail": f"balance {value:,.2f} {currency}",
    }


def _fetch_kimi(api_key: str):
    body = _request_json(KIMI_BALANCE_URL, api_key)
    if not isinstance(body, dict) or "available" not in body:
        return {"error": "unexpected-response"}
    available = _safe_float(body.get("available"))
    voucher = _safe_float(body.get("voucher"))
    cash = _safe_float(body.get("cash"))
    return {
        "kind": "balance",
        "label": f"¥{available:,.2f}",
        "value": round(available, 2),
        "currency": "CNY",
        "detail": f"balance ¥{available:,.2f} (voucher ¥{voucher:,.2f}, cash ¥{cash:,.2f})",
    }


def _fetch_novita(api_key: str):
    body = _request_json(NOVITA_BALANCE_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    balance = _extract_balance(body, ("balance", "credits", "remaining", "available"))
    if balance is None:
        return {"error": "unexpected-response"}
    return {
        "kind": "balance",
        "label": f"${balance:,.2f}",
        "value": round(balance, 2),
        "currency": "USD",
        "detail": f"balance ${balance:,.2f}",
    }


def _fetch_zai(api_key: str):
    body = _request_json(ZAI_BALANCE_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    balance = _extract_balance(body, ("balance", "remaining", "available", "quota"), unwrap_data=True)
    if balance is None:
        return {"error": "unexpected-response"}
    return {
        "kind": "balance",
        "label": f"¥{balance:,.2f}",
        "value": round(balance, 2),
        "currency": "CNY",
        "detail": f"balance ¥{balance:,.2f}",
    }


def _fetch_alibaba(api_key: str):
    body = _request_json(ALIBABA_BILLING_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    inner = body.get("data") if isinstance(body.get("data"), dict) else body
    balance = _extract_balance(body, ("balance", "remaining", "available", "total_cost", "quota"), unwrap_data=True)
    if balance is None:
        return {"error": "unexpected-response"}
    currency = str(inner.get("currency", "CNY")) if isinstance(inner, dict) else "CNY"
    return {
        "kind": "balance",
        "label": f"{balance:,.2f} {currency}",
        "value": round(balance, 2),
        "currency": currency,
        "detail": f"balance {balance:,.2f} {currency}",
    }


def _fetch_arcee(api_key: str):
    body = _request_json(ARCEE_BALANCE_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    balance = _extract_balance(body, ("balance", "credits", "remaining", "available"))
    if balance is None:
        return {"error": "unexpected-response"}
    return {
        "kind": "balance",
        "label": f"${balance:,.2f}",
        "value": round(balance, 2),
        "currency": "USD",
        "detail": f"balance ${balance:,.2f}",
    }


OMARCHY_USAGE_DIR = "~/.local/state/omarchy/agents/usage"


def _short_window_label(label):
    """Compress a collector limit label ("Session (5-hour)", "5h window",
    "Weekly limit") into the short badge the panel renders ("5h", "W", "M")."""
    text = (label or "").lower()
    if "month" in text:
        return "M"
    if "week" in text or "7-day" in text or "seven" in text:
        return "W"
    hours = re.search(r"(\d+)\s*-?\s*h", text)
    if hours:
        return hours.group(1) + "h"
    minutes = re.search(r"(\d+)\s*m", text)
    if minutes:
        return minutes.group(1) + "m"
    return (label or "?")[:6]


def _fetch_collector(agent_id):
    """Read an Omarchy agent-usage collector record (~/.local/state/omarchy/
    agents/usage/<id>.json) — the same source the built-in Agents widget uses,
    so Claude/Codex usage shows up with no API keys. Providers without a
    record report an error and stay hidden (configured: false)."""
    path = os.path.expanduser(OMARCHY_USAGE_DIR + "/" + agent_id + ".json")
    if not os.path.isfile(path):
        return {"error": "no-usage-record"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"error": "unexpected-response"}
    limits = record.get("limits") if isinstance(record, dict) else None
    windows = []
    for entry in limits if isinstance(limits, list) else []:
        if not isinstance(entry, dict) or entry.get("percent") is None:
            continue
        try:
            percent = round(float(entry["percent"]), 1)
        except (TypeError, ValueError):
            continue
        short = _short_window_label(entry.get("title") or entry.get("label"))
        resets = entry.get("resetsAt")
        windows.append({
            "id": short.lower(),
            "label": short,
            "percent": percent,
            "resetsAt": resets if isinstance(resets, str) else None,
        })
    balance = record.get("balance") if isinstance(record, dict) else None
    out = {"kind": "percent" if windows else "note", "windows": windows}
    if isinstance(balance, dict) and _safe_float(balance.get("funded")) > 0:
        funded = _safe_float(balance.get("funded"))
        remaining = max(0.0, _safe_float(balance.get("remaining")))
        currency = str(balance.get("currency") or "USD")
        symbol = "$" if currency.upper() == "USD" else currency + " "
        used = max(0.0, funded - remaining)
        pct_used = used / funded * 100.0
        out.update({
            "kind": "balance",
            "label": f"{symbol}{remaining:,.2f}",
            "value": round(remaining, 2),
            "used": round(used, 2),
            "total": round(funded, 2),
            "ratio": round(pct_used / 100.0, 4),
            "detail": f"{symbol}{remaining:,.2f} left of {symbol}{funded:,.2f}",
        })
    if not windows and out["kind"] != "balance":
        return {"error": "no-usage-data"}
    return out


def _fetch_claude():
    return _fetch_collector("claude")


def _fetch_codex():
    return _fetch_collector("codex")


PROVIDER_SPECS = [
    {"id": "opencode", "name": "OpenCode Go", "display": "OC", "key_envs": ["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"], "fetch": _fetch_opencode},
    {"id": "openrouter", "name": "OpenRouter", "display": "OR", "key_envs": ["OPENROUTER_API_KEY"], "fetch": _fetch_openrouter},
    {"id": "claude", "name": "Claude Code", "display": "CL", "local": True, "fetch": _fetch_claude},
    {"id": "codex", "name": "Codex", "display": "CX", "local": True, "fetch": _fetch_codex},
    {"id": "deepseek", "name": "DeepSeek", "display": "DS", "key_envs": ["DEEPSEEK_API_KEY"], "fetch": _fetch_deepseek},
    {"id": "kimi", "name": "Kimi", "display": "KI", "key_envs": ["KIMI_API_KEY", "MOONSHOT_API_KEY"], "fetch": _fetch_kimi},
    {"id": "novita", "name": "NovitaAI", "display": "NV", "key_envs": ["NOVITA_API_KEY"], "fetch": _fetch_novita},
    {"id": "zai", "name": "ZAI", "display": "Z", "key_envs": ["ZAI_API_KEY", "GLM_API_KEY"], "fetch": _fetch_zai},
    {"id": "alibaba", "name": "Alibaba", "display": "AB", "key_envs": ["DASHSCOPE_API_KEY"], "fetch": _fetch_alibaba},
    {"id": "arcee", "name": "Arcee AI", "display": "AR", "key_envs": ["ARCEE_API_KEY"], "fetch": _fetch_arcee},
]


def _transport_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"http-{exc.code}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "network-error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, ValueError)):
        return "unexpected-response"
    return "unknown-error"


def load_hermes_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a Hermes .env file into os.environ."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Hermes provider usage/balance as JSON.")
    parser.add_argument("--env", default=os.path.expanduser("~/.hermes/.env"),
                        help="Path to the Hermes profile .env file with provider API keys.")
    args = parser.parse_args()

    load_hermes_dotenv(args.env)

    def run(spec):
        rec = {
            "id": spec["id"],
            "name": spec["name"],
            "display": spec["display"],
        }
        if spec.get("local"):
            # Collector-backed provider: no API key, data comes from the
            # Omarchy agent usage records on this machine.
            data = spec["fetch"]()
            rec["configured"] = "error" not in data
            if "error" in data:
                rec["error"] = data["error"]
            else:
                rec.update(data)
            return rec
        key = next((_read_key(e) for e in spec["key_envs"] if _read_key(e)), None)
        rec["configured"] = key is not None
        if not key:
            rec["error"] = "no-key"
            return rec
        try:
            rec.update(spec["fetch"](key))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
            rec["error"] = _transport_error(exc)
        return rec

    results = {}
    with ThreadPoolExecutor(max_workers=len(PROVIDER_SPECS)) as pool:
        futures = {pool.submit(run, spec): spec for spec in PROVIDER_SPECS}
        for fut in as_completed(futures):
            rec = fut.result()
            results[rec["id"]] = rec

    providers = [results[spec["id"]] for spec in PROVIDER_SPECS]
    print(json.dumps(
        {"providers": providers, "generatedAt": int(time.time())},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
