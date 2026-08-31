#!/usr/bin/env python3
"""Hermes provider usage/balance fetcher for the Omarchy bar plugin.

Replicates the fetch half of meviusisback/usage-stats (Hermes Desktop plugin):
reads provider API keys from the Hermes profile .env, queries each vendor's
usage/balance endpoint in parallel, and prints a JSON document the QML panel
renders. Stdlib-only (urllib) so it runs under the system python3 with no deps.

Provider mapping (display -> vendor -> metric):
  OC OpenCode Go      % used (rolling 5h / weekly / monthly)
  OR OpenRouter       USD credits remaining
  CC Command Code     % used (rolling 5h / weekly) + USD balance remaining
  CL Claude Code      collector-backed
  CX Codex            collector-backed
  DS DeepSeek         USD balance (env var or native config fallback)
  KI Kimi/Moonshot    CNY balance (env var or native config fallback)
  NV NovitaAI         USD balance
  Z  ZAI/Zhipu        CNY balance
  AB Alibaba/DashScope CNY usage
  AR Arcee AI         USD balance
  CP GitHub Copilot   % used (OAuth token, opt-in)
  CU Cursor           USD plan spend (OAuth token, opt-in)
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import re
import ssl
import stat
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# Endpoints + transport
# --------------------------------------------------------------------------- #
USAGE_API_URL = "https://opencode.ai/zen/go/v1/usage"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
KIMI_BALANCE_URL = "https://api.moonshot.cn/v1/users/me/balance"
NOVITA_BALANCE_URL = "https://api.novita.ai/openapi/v1/billing/balance/detail"
ZAI_BALANCE_URL = "https://open.bigmodel.cn/api/paas/v4/user/balance"
ALIBABA_BILLING_URL = "https://dashscope.aliyuncs.com/api/v1/quotas"
ARCEE_BALANCE_URL = "https://api.arcee.ai/v2/user/balance"
COMMANDCODE_CREDITS_URL = "https://api.commandcode.ai/alpha/billing/credits"
COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
CURSOR_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

COPILOT_EDITOR_VERSION = "OmarchyUsageMonitor/1.0"
COPILOT_INTEGRATION_ID = "omarchy-ai-subs"

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


# --------------------------------------------------------------------------- #
# SentinelToken — OAuth token wrapper that prevents accidental leakage
# --------------------------------------------------------------------------- #
class SentinelToken:
    """Wraps an OAuth token so repr/str never leak the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "<REDACTED>"

    def __str__(self) -> str:
        return "<REDACTED>"

    def __bool__(self) -> bool:
        return bool(self._value)

    @property
    def value(self) -> str:
        return self._value


# --------------------------------------------------------------------------- #
# File integrity — symlink / permissions / ownership checks
# --------------------------------------------------------------------------- #
def _check_file_integrity(path: str, expected_dir: str) -> bool:
    """Verify a credential file is safe to read: regular file, owned by us,
    not world-readable/writable, and not a symlink escaping expected_dir."""
    try:
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(expected_dir) + os.sep
                              if not os.path.isdir(os.path.realpath(expected_dir))
                              else os.path.realpath(expected_dir) + os.sep):
            return False
        st = os.stat(real)
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_uid != os.getuid():
            return False
        if st.st_mode & 0o077:  # world- or group-readable/writable
            return False
        parent = os.stat(os.path.dirname(real))
        if parent.st_mode & stat.S_IWOTH:  # parent world-writable
            return False
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Native credential readers — JSON and TOML
# --------------------------------------------------------------------------- #
_MAX_NATIVE_BYTES = 65536


def _read_native_key(file_path: str, key_paths: list[list[str]]) -> str | None:
    """Read an API key from a JSON auth file.  *key_paths* is a list of
    path walks to try in order (e.g. [["opencode-go", "key"]])."""
    expanded = os.path.expanduser(file_path)
    expected_dir = os.path.dirname(expanded)
    if not _check_file_integrity(expanded, expected_dir):
        return None
    try:
        real = os.path.realpath(os.path.expanduser(file_path))
        if not os.path.isfile(real):
            return None
        if os.path.getsize(real) > _MAX_NATIVE_BYTES:
            return None
        with open(real, "r", encoding="utf-8") as fh:
            raw = fh.read(_MAX_NATIVE_BYTES + 1)
        if len(raw) > _MAX_NATIVE_BYTES:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    for path in key_paths:
        val = data
        for segment in path:
            if not isinstance(val, dict):
                val = None
                break
            val = val.get(segment)
        if isinstance(val, str) and val:
            return val
    return None


def _read_toml_key(file_path: str, key_path: list[str]) -> str | None:
    """Read an API key from a TOML config file (DeepSeek, Kimi)."""
    if tomllib is None:
        return None
    expanded = os.path.expanduser(file_path)
    expected_dir = os.path.dirname(expanded)
    if not _check_file_integrity(expanded, expected_dir):
        return None
    try:
        real = os.path.realpath(os.path.expanduser(file_path))
        if not os.path.isfile(real):
            return None
        if os.path.getsize(real) > _MAX_NATIVE_BYTES:
            return None
        with open(real, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError if tomllib else OSError, ValueError):
        return None

    val = data
    for segment in key_path:
        if not isinstance(val, dict):
            return None
        val = val.get(segment)
    if isinstance(val, str) and val:
        return val
    return None


def _read_oauth_token(file_path: str, key_path: list[str],
                       expected_dir: str) -> SentinelToken | None:
    """Read an OAuth token and wrap it in SentinelToken for safe handling.
    Returns None if the file fails integrity checks."""
    if not _check_file_integrity(file_path, expected_dir):
        return None
    try:
        real = os.path.realpath(os.path.expanduser(file_path))
        if os.path.getsize(real) > _MAX_NATIVE_BYTES:
            return None
        with open(real, "r", encoding="utf-8") as fh:
            raw = fh.read(_MAX_NATIVE_BYTES + 1)
        if len(raw) > _MAX_NATIVE_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    val = data
    for segment in key_path:
        if not isinstance(val, dict):
            return None
        val = val.get(segment)
    if isinstance(val, str) and val:
        return SentinelToken(val)
    return None


def _read_github_oauth_token() -> SentinelToken | None:
    """Read a GitHub OAuth token from gh CLI config or Copilot editor config."""
    # Priority 1: GITHUB_TOKEN env var
    env_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_token:
        return SentinelToken(env_token)

    gh_config = os.path.expanduser("~/.config/gh/hosts.yml")
    if os.path.isfile(gh_config) and _check_file_integrity(gh_config, os.path.expanduser("~/.config/gh")):
        try:
            with open(gh_config, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("oauth_token:"):
                        _, _, val = line.partition(":")
                        val = val.strip().strip("\"'")
                        if val:
                            return SentinelToken(val)
        except OSError:
            pass

    apps_json = os.path.expanduser("~/.config/github-copilot/apps.json")
    if os.path.isfile(apps_json):
        token = _read_oauth_token(apps_json, ["github.com", "oauth_token"],
                                  os.path.expanduser("~/.config/github-copilot"))
        if token:
            return token

    return None


def _read_cursor_token() -> SentinelToken | None:
    """Read the Cursor access token from its local SQLite state DB."""
    db_path = os.path.expanduser("~/.cursor/state.vscdb")
    if not _check_file_integrity(db_path, os.path.expanduser("~/.cursor")):
        return None
    try:
        real = os.path.realpath(db_path)
        if os.path.getsize(real) > 10 * 1024 * 1024:  # 10MB cap
            return None
        uri = f"file:{real}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cur = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                ("cursorAuth/cachedToken",),
            )
            row = cur.fetchone()
            if row and isinstance(row[0], str) and row[0]:
                return SentinelToken(row[0])
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        pass
    return None


# --------------------------------------------------------------------------- #
# Error response scrubbing — strip token patterns before display
# --------------------------------------------------------------------------- #
_TOKEN_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]{36,}|"
    r"cur_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9\-_]{20,}|sk-ant-[A-Za-z0-9\-_]{20,})"
)


def _scrub_error(text: str) -> str:
    """Replace token-like patterns with <REDACTED>."""
    return _TOKEN_PATTERN.sub("<REDACTED>", text)


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same origin (scheme + host) so the
    Authorization (API key) header is never forwarded elsewhere and never
    downgraded to plaintext. Any other redirect raises HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.request.urlparse(newurl)
        old = urllib.request.urlparse(req.full_url)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise urllib.error.HTTPError(
                req.full_url, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DEFAULT_SSL_CONTEXT = ssl.create_default_context()
_OPENER = urllib.request.build_opener(
    _SameOriginRedirectHandler(),
    urllib.request.HTTPSHandler(context=_DEFAULT_SSL_CONTEXT),
)


def _request_json(url: str, api_key: str):
    # OpenCode's edge rejects the default Python-urllib User-Agent with 403,
    # so we send a browser-like one everywhere. The Authorization header is
    # never forwarded off-origin (see _SameOriginRedirectHandler).
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
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
            percent = round(_finite(raw["percent"]), 1) if raw.get("percent") is not None else None
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


def _finite(value, default: float = 0.0) -> float:
    """Like _safe_float, but rejects NaN/inf.

    json.dumps() emits bare NaN/Infinity for non-finite floats, which is not
    valid JSON — QML's JSON.parse would throw on the whole document and every
    provider block would go blank. Keep those values out of the output.
    """
    number = _safe_float(value, default)
    return number if math.isfinite(number) else default


_CURRENCY_RE = re.compile(r"^[A-Za-z]{3,5}$")


def _safe_currency(value, default: str = "USD") -> str:
    """Restrict an API-supplied currency code to a short alphanumeric token.
    The value is rendered by QML Text labels, so anything outside the shape
    of a currency code falls back to the default instead of reaching the UI."""
    text = str(value or "").strip()
    return text.upper() if _CURRENCY_RE.fullmatch(text) else default


def _extract_balance(body, field_names, *, unwrap_data: bool = False):
    inner = body.get("data") if unwrap_data and isinstance(body.get("data"), dict) else body
    for key in field_names:
        if key in inner:
            return _finite(inner[key])
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
    total = _finite(data["total_credits"])
    used = _finite(data["total_usage"])
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
        currency = _safe_currency(info.get("currency"), currency)
        total += _finite(info.get("total_balance"))
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
    if not isinstance(body, dict) or body.get("code") != 0:
        return {"error": "unexpected-response"}
    data = body.get("data")
    if not isinstance(data, dict) or "available_balance" not in data:
        return {"error": "unexpected-response"}
    available = _finite(data.get("available_balance"))
    voucher = _finite(data.get("voucher_balance"))
    cash = _finite(data.get("cash_balance"))
    return {
        "kind": "balance",
        "label": f"${available:,.2f}",
        "value": round(available, 2),
        "currency": "USD",
        "detail": f"balance ${available:,.2f} (voucher ${voucher:,.2f}, cash ${cash:,.2f})",
    }


def _fetch_novita(api_key: str):
    body = _request_json(NOVITA_BALANCE_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    # Novita reports balance in 1/10000 USD, so 10000 == $1.00.
    raw = body.get("availableBalance") or body.get("cashBalance")
    if raw is None:
        return {"error": "unexpected-response"}
    balance = _finite(raw) / 10000.0
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
    if body.get("code") not in (None, "Success"):
        return {"error": "unexpected-response"}
    data = body.get("data")
    if not isinstance(data, dict):
        return {"error": "unexpected-response"}
    # DashScope reports account balances in USD. Prefer available credit, then
    # credits.
    raw = data.get("available")
    if raw is None:
        raw = data.get("credits")
    if raw is None:
        return {"error": "unexpected-response"}
    balance = _finite(raw)
    return {
        "kind": "balance",
        "label": f"${balance:,.2f}",
        "value": round(balance, 2),
        "currency": "USD",
        "detail": f"balance ${balance:,.2f}",
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


def _fetch_commandcode(api_key: str):
    body = _request_json(COMMANDCODE_CREDITS_URL, api_key)
    if not isinstance(body, dict):
        return {"error": "unexpected-response"}
    credits = body.get("credits") if isinstance(body.get("credits"), dict) else None
    window_limits = body.get("windowLimits") if isinstance(body.get("windowLimits"), dict) else None
    if credits is None and window_limits is None:
        return {"error": "unexpected-response"}

    # Monthly remaining credits (USD)
    monthly_credits = _finite(credits.get("monthlyCredits")) if credits else None
    purchased = _finite(credits.get("purchasedCredits")) if credits else 0.0
    free = _finite(credits.get("freeCredits")) if credits else 0.0
    # _finite on the sum too: two large-but-finite components can still add
    # up to inf, which would serialize as bare Infinity.
    total_remaining = max(0.0, _finite((monthly_credits or 0.0) + purchased + free))

    # Rolling windows: fiveHour and weekly — each has used/cap/resetAt (epoch ms)
    windows = []
    window_map = {"fiveHour": "5h", "weekly": "W"}
    for raw_key, label in window_map.items():
        wl = window_limits.get(raw_key) if isinstance(window_limits, dict) else None
        if not isinstance(wl, dict):
            continue
        cap = _finite(wl.get("cap"))
        used = _finite(wl.get("used"))
        if cap <= 0:
            continue
        pct = round(min(100.0, max(0.0, used / cap * 100.0)), 1)
        # resetAt is epoch ms — convert to ISO string for the QML countdown
        reset_ms = wl.get("resetAt")
        reset_iso = None
        if isinstance(reset_ms, (int, float)) and reset_ms > 0:
            try:
                reset_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset_ms / 1000.0))
            except (ValueError, OverflowError, OSError):
                # Out-of-range timestamp: lose the countdown, keep the window.
                reset_iso = None
        windows.append({
            "id": raw_key.lower(),
            "label": label,
            "percent": pct,
            "resetsAt": reset_iso,
        })

    # Rolling windows render as percent kind (like OpenCode).
    # Show the monthly balance in the detail line.
    if windows:
        headline_pct = windows[0].get("percent") if windows else None
        detail_parts = [f"{w['label']} {_pct(w['percent'])}" for w in windows]
        if total_remaining > 0:
            detail_parts.append(f"${total_remaining:,.2f} remaining")
        return {
            "kind": "percent",
            "label": _pct(headline_pct),
            "value": headline_pct,
            "detail": " · ".join(detail_parts),
            "windows": windows,
            "monthlyCredits": round(total_remaining, 2) if total_remaining > 0 else None,
        }

    # Fallback: no rolling windows, just show balance
    if total_remaining > 0:
        return {
            "kind": "balance",
            "label": f"${total_remaining:,.2f}",
            "value": round(total_remaining, 2),
            "currency": "USD",
            "detail": f"balance ${total_remaining:,.2f}",
        }

    return {"error": "no-usage-data"}


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
    # Fallback: keep only markup-safe characters. The label reaches QML Text
    # sinks (including the bar chip's WidgetButton, which renders AutoText and
    # exposes no textFormat override), so arbitrary file content must never
    # pass through verbatim.
    return re.sub(r"[^A-Za-z0-9 ._-]", "", (label or ""))[:6] or "?"


def _fetch_collector(agent_id):
    """Read an Omarchy agent-usage collector record (~/.local/state/omarchy/
    agents/usage/<id>.json) — the same source the built-in Agents widget uses,
    so Claude/Codex usage shows up with no API keys. Providers without a
    record report an error and stay hidden (configured: false)."""
    path = os.path.expanduser(OMARCHY_USAGE_DIR + "/" + agent_id + ".json")
    if not os.path.isfile(path):
        return {"error": "no-usage-record"}
    if os.path.getsize(path) > _MAX_NATIVE_BYTES:
        return {"error": "file-too-large"}
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
            percent = round(_finite(entry["percent"]), 1)
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
    if isinstance(balance, dict) and _finite(balance.get("funded")) > 0:
        funded = _finite(balance.get("funded"))
        remaining = max(0.0, _finite(balance.get("remaining")))
        currency = _safe_currency(balance.get("currency"))
        symbol = "$" if currency == "USD" else currency + " "
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


# --------------------------------------------------------------------------- #
# OAuth-backed providers — Copilot and Cursor (Tier 2, read-use-discard)
# --------------------------------------------------------------------------- #
def _fetch_copilot(token: SentinelToken):
    """Query GitHub's internal Copilot usage endpoint."""
    real_token = token.value
    try:
        request = urllib.request.Request(
            COPILOT_USER_URL,
            headers={
                "Authorization": f"Bearer {real_token}",
                "Accept": "application/json",
                "Editor-Version": COPILOT_EDITOR_VERSION,
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "User-Agent": USER_AGENT,
            },
        )
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"error": "response-too-large"}
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"error": _scrub_error(f"http-{exc.code}")}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"error": "network-error"}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"error": "unexpected-response"}
    finally:
        del real_token
        gc.collect()

    segments = body.get("segments", []) if isinstance(body, dict) else []
    chat_used = None
    completion_used = None
    plan = body.get("plan", "") if isinstance(body, dict) else ""

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("kind", "")
        if kind == "chat":
            chat_used = seg.get("percent_used")
        elif kind == "code_completion":
            completion_used = seg.get("percent_used")

    parts = []
    if chat_used is not None:
        parts.append(f"chat {_pct(chat_used)}")
    if completion_used is not None:
        parts.append(f"completions {_pct(completion_used)}")

    if not parts:
        return {"error": "no-usage-data"}

    return {
        "kind": "percent",
        "label": parts[0] if len(parts) == 1 else " · ".join(parts),
        "detail": f"{plan} — {' · '.join(parts)}" if plan else " · ".join(parts),
        "windows": [],
    }


def _fetch_cursor(token: SentinelToken):
    """Query Cursor's dashboard API for plan usage."""
    real_token = token.value
    try:
        payload = json.dumps({}).encode("utf-8")
        request = urllib.request.Request(
            CURSOR_USAGE_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {real_token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"error": "response-too-large"}
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"error": _scrub_error(f"http-{exc.code}")}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"error": "network-error"}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"error": "unexpected-response"}
    finally:
        del real_token
        gc.collect()

    if not isinstance(body, dict):
        return {"error": "unexpected-response"}

    plan_usage = body.get("planUsage", {})
    if not isinstance(plan_usage, dict):
        return {"error": "no-usage-data"}

    total_cents = plan_usage.get("totalSpend", 0)
    limit_cents = plan_usage.get("limit", 0)
    included_cents = plan_usage.get("includedSpend", 0)

    if limit_cents and limit_cents > 0:
        total_usd = total_cents / 100.0
        limit_usd = limit_cents / 100.0
        pct = round(total_cents / limit_cents * 100.0, 1) if limit_cents else 0
        remaining = max(0.0, limit_usd - total_usd)
        return {
            "kind": "balance",
            "label": f"${remaining:,.2f} left",
            "value": round(remaining, 2),
            "used": round(total_usd, 2),
            "total": round(limit_usd, 2),
            "ratio": round(pct / 100.0, 4),
            "detail": f"${total_usd:,.2f} of ${limit_usd:,.2f} used ({pct:.0f}%)",
        }

    if included_cents and included_cents > 0:
        total_usd = total_cents / 100.0
        included_usd = included_cents / 100.0
        return {
            "kind": "balance",
            "label": f"${total_usd:,.2f} used",
            "value": round(total_usd, 2),
            "total": round(included_usd, 2),
            "detail": f"${total_usd:,.2f} of ${included_usd:,.2f} included",
        }

    return {"error": "no-usage-data"}


PROVIDER_SPECS = [
    {"id": "opencode", "name": "OpenCode Go", "display": "OC", "logo": "opencode", "key_envs": ["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"], "fetch": _fetch_opencode},
    {"id": "openrouter", "name": "OpenRouter", "display": "OR", "logo": "openrouter", "key_envs": ["OPENROUTER_API_KEY"], "fetch": _fetch_openrouter},
    {"id": "claude", "name": "Claude Code", "display": "CL", "logo": "claude", "local": True, "fetch": _fetch_claude},
    {"id": "codex", "name": "Codex", "display": "CX", "logo": "openai", "local": True, "fetch": _fetch_codex},
    {"id": "deepseek", "name": "DeepSeek", "display": "DS", "logo": "deepseek", "key_envs": ["DEEPSEEK_API_KEY"], "native_toml": {"file": "~/.deepseek/config.toml", "path": ["api_key"]}, "fetch": _fetch_deepseek},
    {"id": "kimi", "name": "Kimi", "display": "KI", "logo": "kimi", "key_envs": ["KIMI_API_KEY", "MOONSHOT_API_KEY"], "native_toml": {"file": "~/.kimi-code/config.toml", "path": ["providers", "kimi", "api_key"]}, "fetch": _fetch_kimi},
    {"id": "novita", "name": "NovitaAI", "display": "NV", "logo": "novita", "key_envs": ["NOVITA_API_KEY"], "fetch": _fetch_novita},
    {"id": "zai", "name": "ZAI", "display": "Z", "logo": "zai", "key_envs": ["ZAI_API_KEY", "GLM_API_KEY"], "fetch": _fetch_zai},
    {"id": "alibaba", "name": "Alibaba", "display": "AB", "logo": "alibabacloud", "key_envs": ["DASHSCOPE_API_KEY"], "fetch": _fetch_alibaba},
    {"id": "arcee", "name": "Arcee AI", "display": "AR", "logo": "arcee", "key_envs": ["ARCEE_API_KEY"], "fetch": _fetch_arcee},
    {"id": "commandcode", "name": "Command Code", "display": "CC", "logo": "commandcode", "key_envs": ["COMMANDCODE_API_KEY"], "fetch": _fetch_commandcode},
    # Tier 2 — OAuth-backed providers (read-use-discard, opt-in)
    {"id": "copilot", "name": "GitHub Copilot", "display": "CP", "logo": "openai", "oauth": True, "token_reader": _read_github_oauth_token, "fetch": _fetch_copilot},
    {"id": "cursor", "name": "Cursor", "display": "CU", "logo": "opencode", "oauth": True, "token_reader": _read_cursor_token, "fetch": _fetch_cursor},
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
    """Load KEY=VALUE pairs from a Hermes .env file into os.environ.

    Handles common dotenv idioms: an optional leading ``export``, single- or
    double-quoted values (including a ``#`` inside quotes), and a trailing
    ``# comment`` on unquoted values.
    """
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            line = re.sub(r"^export\s+", "", line)
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value and value[0] in "\"'":
                # Quoted value: capture up to the matching closing quote.
                quote = value[0]
                rest = value[1:]
                end = rest.find(quote)
                if end != -1:
                    value = rest[:end]
                else:
                    value = rest
            else:
                # Unquoted value: the first unescaped ' #' starts a comment.
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            if key:
                os.environ[key] = value.strip()


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
            "logo": spec.get("logo", ""),
        }
        if spec.get("local"):
            # Collector-backed provider: no API key, data comes from the
            # Omarchy agent usage records on this machine.
            try:
                data = spec["fetch"]()
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
                rec["error"] = _transport_error(exc)
                rec["configured"] = False
                return rec
            rec["configured"] = "error" not in data
            if "error" in data:
                rec["error"] = data["error"]
            else:
                rec.update(data)
            return rec

        # Tier 2 — OAuth-backed providers: read-use-discard, never cached
        if spec.get("oauth"):
            token = spec["token_reader"]()
            if not token:
                rec["configured"] = False
                rec["error"] = "no-token"
                return rec
            try:
                rec.update(spec["fetch"](token))
            except Exception as exc:  # noqa: BLE001
                rec["error"] = _scrub_error(_transport_error(exc))
            finally:
                token = None  # noqa: F841 — read-use-discard
                gc.collect()
            rec["configured"] = "error" not in rec
            return rec

        # Tier 1 — Scoped API keys: env var → native config file fallback
        key = next((_read_key(e) for e in spec["key_envs"] if _read_key(e)), None)

        # Native TOML fallback (DeepSeek, Kimi)
        if not key and spec.get("native_toml"):
            ntl = spec["native_toml"]
            key = _read_toml_key(ntl["file"], ntl["path"])

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
