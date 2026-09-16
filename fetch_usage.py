#!/usr/bin/env python3
"""Hermes provider usage/balance fetcher for the Omarchy bar plugin.

Replicates the fetch half of meviusisback/usage-stats (Hermes Desktop plugin):
reads provider API keys from the Hermes profile .env, queries each vendor's
usage/balance endpoint in parallel, and prints a JSON document the QML panel
renders. Stdlib-only (urllib) so it runs under the system python3 with no deps.

Every CREDENTIAL file (the --env file, native config files, OAuth token stores)
is confined to a trusted directory and validated before it is read — regular
single-linked file, owned by the user, no group/other permission bits, size cap,
no symlink at the path, verified descriptor.  See ``confined_path``.  The one
deliberate exception is ``_fetch_collector``: it reads non-secret Omarchy usage
records from a path built from a constant agent id, not from configuration.

Provider mapping (display -> vendor -> metric):
  OC OpenCode Go      % used (rolling 5h / weekly / monthly)
  OR OpenRouter       USD credits remaining
  CC Command Code     % used (rolling 5h / weekly / monthly) + USD balance remaining
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
import contextlib
import datetime
import gc
import json
import math
import os
import re
import ssl
import stat
import sqlite3
import sys
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
COMMANDCODE_SUBSCRIPTIONS_URL = "https://api.commandcode.ai/alpha/billing/subscriptions"
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
COMMANDCODE_PLAN_ALLOWANCES = {
    "individual-go": 10.0,
    "individual-pro": 30.0,
    "individual-goat": 70.0,
    "individual-pro-v1": 80.0,
    "individual-max": 150.0,
    "individual-ultra": 300.0,
    "go": 10.0,
    "pro": 30.0,
    "goat": 70.0,
    "max": 150.0,
    "ultra": 300.0,
}


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
# Confined credential reads — one trust implementation for every secret file
# --------------------------------------------------------------------------- #
_MAX_NATIVE_BYTES = 65536
MAX_CREDENTIAL_BYTES = 262144  # 256 KiB — far above any real .env/credential file
_PROC_FD_PREFIX = "/proc/self/fd/"


class CredentialFileError(Exception):
    """A credential file was refused by the confinement or validation rules."""


def _home_dir() -> str | None:
    """The real home directory: ``$HOME`` when it is absolute and exists, else
    the passwd entry.  ``os.path.expanduser("~")`` yields ``/`` for an empty
    ``HOME``, which would silently relocate every credential path, and a home of
    ``/`` would make every absolute path look like it is "inside the home"."""
    home = os.environ.get("HOME", "")
    if home and home != os.sep and os.path.isabs(home) and os.path.isdir(home):
        return home
    try:
        import pwd

        candidate = pwd.getpwuid(os.getuid()).pw_dir or None
        return candidate if candidate and candidate != os.sep else None
    except (ImportError, KeyError, OSError):
        return None


def _expand_home(path: str) -> str:
    """``~`` expansion anchored on a validated home directory.

    Only a bare ``~`` or ``~/…`` is expanded; ``~user`` is left untouched (it
    names another account, and ``confined_path`` then refuses it as a
    non-absolute path) instead of being mangled into ``/home/<user>user/…``."""
    if isinstance(path, str) and (path == "~" or path.startswith("~/")):
        home = _home_dir()
        if not home:
            return path
        return home + path[1:]
    return path


def _is_within(child: str, parent: str) -> bool:
    """True when realpath *child* is strictly inside directory *parent*."""
    try:
        return os.path.commonpath([child, parent]) == parent and child != parent
    except ValueError:  # mixed absolute/relative paths
        return False


def _dir_chain_is_trusted(path: str, uid: int, ancestor_uids, trust_root: str) -> bool:
    """Every directory from *path* up to *trust_root* must be owned by *uid* or
    a trusted ancestor uid and must not be group- or world-writable.  Above
    *trust_root* nothing is examined (tests anchor this at their own fixture)."""
    current = os.path.realpath(path)
    trust_root = os.path.realpath(trust_root)
    while True:
        try:
            st = os.stat(current)
        except OSError:
            return False
        if not stat.S_ISDIR(st.st_mode):
            return False
        if st.st_uid != uid and st.st_uid not in ancestor_uids:
            return False
        if st.st_mode & 0o022:
            return False
        if current == trust_root:
            return True
        parent = os.path.dirname(current)
        if parent == current:  # reached / without meeting trust_root
            return False
        current = parent


def _trusted_dir(path: str, uid=None, ancestor_uids=(0,), trust_root="/") -> str | None:
    """Realpath of *path* when it is a trustworthy root for credential files: an
    existing directory owned by us, not group/world-writable, with a trusted
    ancestor chain.  ``None`` otherwise."""
    if not isinstance(path, str) or not path:
        return None
    uid = os.getuid() if uid is None else uid
    real = os.path.realpath(_expand_home(path))
    try:
        st = os.stat(real)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if st.st_uid != uid:
        return None
    if st.st_mode & 0o022:
        return None
    if not _dir_chain_is_trusted(real, uid, ancestor_uids, trust_root):
        return None
    return real


def _containing_root(real: str, roots, uid: int, ancestor_uids, trust_root: str) -> str | None:
    """The trusted root that strictly contains *real*, or None."""
    for root in roots if isinstance(roots, (list, tuple)) else []:
        root_real = _trusted_dir(root, uid, ancestor_uids, trust_root)
        if root_real and _is_within(real, root_real):
            return root_real
    return None


def confined_path(path: str, roots, max_bytes: int = _MAX_NATIVE_BYTES, uid=None,
                  ancestor_uids=(0,), trust_root: str = "/") -> str:
    """Validate *path* against *roots* and return the real path to read.

    A credential file must be a regular, single-linked file resolving strictly
    inside one of *roots*, owned by the running user, with no group/other
    permission bits, no larger than *max_bytes*, and reachable through
    directories nobody else can write to.  A symlink at the path itself is
    refused (directory symlinks are resolved by ``realpath`` and must still land
    inside the root).  Raises ``CredentialFileError``."""
    if not isinstance(path, str) or not path.strip():
        raise CredentialFileError("empty path")
    uid = os.getuid() if uid is None else uid
    if "\x00" in path:
        raise CredentialFileError("path contains NUL")
    raw = _expand_home(path.strip())
    if not os.path.isabs(raw):
        raise CredentialFileError("not an absolute path")
    if os.pardir in raw.split(os.sep):
        raise CredentialFileError("path contains '..'")
    # Normalise BEFORE the symlink test: os.path.islink("link/") is False for a
    # symlink to a regular file, so a trailing slash would slip past it.
    raw = os.path.normpath(raw)
    try:
        if os.path.islink(raw):
            raise CredentialFileError("symlink refused")
        real = os.path.realpath(raw)
    except (OSError, ValueError) as exc:
        raise CredentialFileError(f"cannot resolve: {exc}") from exc
    inside = _containing_root(real, roots, uid, ancestor_uids, trust_root)
    if inside is None:
        raise CredentialFileError("outside every trusted credential directory")
    try:
        st = os.stat(real)
    except OSError as exc:
        raise CredentialFileError(f"cannot stat: {exc.strerror or 'error'}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise CredentialFileError("not a regular file")
    if st.st_uid != uid:
        raise CredentialFileError("not owned by the current user")
    if st.st_nlink != 1:
        raise CredentialFileError("multiple hard links")
    if st.st_mode & 0o077:
        raise CredentialFileError("group/other permission bits set")
    if st.st_size > max_bytes:
        raise CredentialFileError("file too large")
    if not _dir_chain_is_trusted(os.path.dirname(real), uid, ancestor_uids, inside):
        raise CredentialFileError("directory chain is not trusted")
    return real


def read_confined_text(path: str, roots, max_bytes: int = MAX_CREDENTIAL_BYTES,
                       uid=None, ancestor_uids=(0,), trust_root: str = "/") -> str:
    """Read a validated credential file through a verified descriptor.

    ``confined_path`` validates the path; the file is then opened with
    ``O_NOFOLLOW`` and re-validated on the descriptor itself — ``fstat``
    attributes, an identity match (``samestat``) against the validated path, and
    a containment check on the descriptor's real path via ``/proc/self/fd``.
    A path swapped between the two steps is therefore detected unless the swap
    is invisible to a re-stat of the same path *and* procfs is unavailable, in
    which case the read fails closed.  ``O_NONBLOCK`` keeps a FIFO planted at
    the path from wedging the refresh tick.  Raises ``CredentialFileError``."""
    real = confined_path(path, roots, max_bytes, uid, ancestor_uids, trust_root)
    uid = os.getuid() if uid is None else uid
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise CredentialFileError(f"cannot open: {exc.strerror or 'error'}") from exc
    chunks: list[bytes] = []
    try:
        st = os.fstat(fd)
        # The descriptor must be the very file that was validated: a path swapped
        # between the validation and this open cannot redirect the read.  (This
        # catches a swap that is visible at re-stat time; a swap that persists
        # identically for both stats is caught by the containment check below.)
        try:
            same_file = os.path.samestat(st, os.stat(real))
        except OSError:
            same_file = False
        if not same_file:
            raise CredentialFileError("descriptor does not match the validated path")
        if not stat.S_ISREG(st.st_mode):
            raise CredentialFileError("not a regular file")
        if st.st_uid != uid:
            raise CredentialFileError("not owned by the current user")
        if st.st_nlink != 1:
            raise CredentialFileError("multiple hard links")
        if st.st_mode & 0o077:
            raise CredentialFileError("group/other permission bits set")
        if st.st_size > max_bytes:
            raise CredentialFileError("file too large")
        # The descriptor must also resolve inside the trusted tree: this is what
        # catches a swapped directory or a mount boundary, since re-statting the
        # PATH cannot see either.  It needs procfs, so an unresolvable
        # /proc/self/fd fails closed rather than silently dropping the check.
        fd_real = os.path.realpath(_PROC_FD_PREFIX + str(fd))
        if fd_real.startswith(_PROC_FD_PREFIX):
            raise CredentialFileError("cannot verify the descriptor's location")
        if _containing_root(fd_real, roots, uid, ancestor_uids, trust_root) is None:
            raise CredentialFileError("descriptor escaped the trusted directory")
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise CredentialFileError(f"cannot read: {exc.strerror or 'error'}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise CredentialFileError("file too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialFileError("not valid UTF-8") from exc


def hermes_env_roots(uid=None, ancestor_uids=(0,), trust_root: str = "/") -> list[str]:
    """Directories a Hermes ``.env`` may live in: the active profile root
    (``$HERMES_HOME`` when it is set, trustworthy and inside the home directory)
    and the default ``~/.hermes``.  Profile env files
    (``~/.hermes/profiles/<name>/.env``) are covered by the root, so they need
    no separate entry."""
    uid = os.getuid() if uid is None else uid
    home = _home_dir()
    candidates: list[str] = []
    hermes_home = os.environ.get("HERMES_HOME", "")
    if hermes_home and home:
        # Only a profile directory INSIDE the home may widen the roots:
        # HERMES_HOME=$HOME would otherwise turn every private file in the home
        # directory (ssh keys, cloud credentials) into an acceptable credential
        # file for the --env setting.
        if _is_within(os.path.realpath(_expand_home(hermes_home)),
                      os.path.realpath(home)):
            candidates.append(hermes_home)
    if home:
        candidates.append(os.path.join(home, ".hermes"))
    roots: list[str] = []
    for candidate in candidates:
        real = _trusted_dir(candidate, uid, ancestor_uids, trust_root)
        if real and real not in roots:
            roots.append(real)
    return roots


# --------------------------------------------------------------------------- #
# Native credential readers — JSON and TOML
# --------------------------------------------------------------------------- #
def _read_confined_json(file_path: str, roots, max_bytes: int = _MAX_NATIVE_BYTES):
    """Parse a validated JSON credential file.  ``None`` when the file is
    refused, unreadable or not a JSON object."""
    try:
        raw = read_confined_text(file_path, roots, max_bytes)
    except CredentialFileError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml_key(file_path: str, key_path: list[str]) -> str | None:
    """Read an API key from a TOML config file (DeepSeek, Kimi)."""
    if tomllib is None:
        return None
    expanded = _expand_home(file_path)
    try:
        raw = read_confined_text(expanded, [os.path.dirname(expanded)])
    except CredentialFileError:
        return None
    try:
        data = tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, ValueError):
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
    Returns None if the file is refused by the credential-file checks."""
    data = _read_confined_json(file_path, [expected_dir])
    if data is None:
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

    gh_dir = _expand_home("~/.config/gh")
    gh_config = os.path.join(gh_dir, "hosts.yml")
    try:
        raw = read_confined_text(gh_config, [gh_dir])
    except CredentialFileError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("oauth_token:"):
            _, _, val = line.partition(":")
            val = val.strip().strip("\"'")
            if val:
                return SentinelToken(val)

    apps_json = _expand_home("~/.config/github-copilot/apps.json")
    token = _read_oauth_token(apps_json, ["github.com", "oauth_token"],
                              os.path.dirname(apps_json))
    if token:
        return token

    return None


def _read_cursor_token() -> SentinelToken | None:
    """Read the Cursor access token from its local SQLite state DB."""
    db_path = _expand_home("~/.cursor/state.vscdb")
    try:
        real = confined_path(db_path, [os.path.dirname(db_path)],
                             max_bytes=10 * 1024 * 1024)
        if any(ch in real for ch in "?#"):  # would break the SQLite URI below
            return None
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(real, flags)
    except (CredentialFileError, OSError):
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o077):
            return None
        if not os.path.samestat(st, os.stat(real)):
            return None
        # SQLite re-opens whatever name it is given (it resolves /proc/self/fd
        # back to the real name), so the fd pins the inode but does not by
        # itself remove the re-open; the descriptor is kept open and its
        # verification is done here, before SQLite touches the file.
        fd_ref = _PROC_FD_PREFIX + str(fd)
        if not os.path.exists(fd_ref):
            return None  # fail closed rather than hand SQLite a bare path
        conn = sqlite3.connect(f"file:{fd_ref}?mode=ro", uri=True, timeout=5)
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
    except (OSError, sqlite3.Error, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
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


def _normalize_iso_timestamp(ts) -> str | None:
    """Convert an epoch millisecond timestamp (int/float) or ISO string into
    a UTC ISO 8601 string (%Y-%m-%dT%H:%M:%SZ) for QML countdown compatibility."""
    if not ts:
        return None
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000.0))
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(ts, str):
        try:
            clean = ts.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(clean)
            return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            return None
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

    # Rolling windows: fiveHour, weekly, and optional monthly from windowLimits
    windows = []
    window_map = {"fiveHour": "5h", "weekly": "W", "monthly": "M"}
    for raw_key, label in window_map.items():
        wl = window_limits.get(raw_key) if isinstance(window_limits, dict) else None
        if not isinstance(wl, dict):
            continue
        cap = _finite(wl.get("cap"))
        used = _finite(wl.get("used"))
        if cap <= 0:
            continue
        pct = round(min(100.0, max(0.0, used / cap * 100.0)), 1)
        reset_iso = _normalize_iso_timestamp(wl.get("resetAt"))
        windows.append({
            "id": raw_key.lower(),
            "label": label,
            "percent": pct,
            "resetsAt": reset_iso,
        })

    # If windowLimits did not supply a monthly window, enrich it from the
    # subscription plan allowance and billing cycle end date.
    has_monthly = any(w.get("id") == "monthly" or w.get("label") == "M" for w in windows)
    if not has_monthly and monthly_credits is not None:
        try:
            sub_body = _request_json(COMMANDCODE_SUBSCRIPTIONS_URL, api_key)
            sub_data = sub_body.get("data") if isinstance(sub_body, dict) else None
            if isinstance(sub_data, dict):
                status = str(sub_data.get("status", "")).lower()
                plan_id = str(sub_data.get("planId", "")).lower()
                cap = COMMANDCODE_PLAN_ALLOWANCES.get(plan_id)
                if cap and cap > 0 and status in ("active", "trialing"):
                    used = max(0.0, cap - monthly_credits)
                    pct = round(min(100.0, max(0.0, used / cap * 100.0)), 1)
                    reset_iso = _normalize_iso_timestamp(sub_data.get("currentPeriodEnd"))
                    windows.append({
                        "id": "monthly",
                        "label": "M",
                        "percent": pct,
                        "resetsAt": reset_iso,
                    })
        except Exception:
            # Subscription enrichment is best-effort; keep existing windows on error.
            pass

    # Rolling windows render as percent kind (like OpenCode).
    # Show the monthly balance in the detail line.
    if windows:
        headline_pct = next((w["percent"] for w in windows if w.get("percent") is not None), None)
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
            # Omarchy's agent collectors write a FRACTION (0..1) into a field
            # named "percent": normalize_utilization() returns min(1.0, n/100).
            # Scale to a real percentage, or Claude/Codex read 100x too low
            # (71% of the weekly limit rendering as "0.7%").
            raw_pct = _finite(entry["percent"])
            percent = round(raw_pct * 100.0, 1) if raw_pct <= 1.0 else round(raw_pct, 1)
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


# Environment variables a credential file must never set: they choose which code
# runs, where paths resolve, which TLS trust store or proxy is used, not which
# key is used.  See load_hermes_dotenv.
_ENV_DENYLIST = frozenset({
    "PATH", "HOME", "IFS", "ENV", "BASH_ENV", "SHELLOPTS",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "HERMES_HOME", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "GCONV_PATH", "LOCPATH",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def load_hermes_dotenv(path: str, roots=None, max_bytes: int = MAX_CREDENTIAL_BYTES,
                       uid=None, ancestor_uids=(0,), trust_root: str = "/") -> bool:
    """Load KEY=VALUE pairs from a Hermes .env file into os.environ.

    The file is confined to *roots* (the Hermes profile directories by default)
    and validated before a byte is read — see ``confined_path``.  A refused file
    is reported once on stderr (which the panel captures separately from stdout)
    and skipped: the widget still renders, providers simply report ``no-key``.

    Handles common dotenv idioms: an optional leading ``export``, single- or
    double-quoted values (including a ``#`` inside quotes), and a trailing
    ``# comment`` on unquoted values.  Returns True when the file was read.
    The trust parameters are injectable so the rules are testable without root.
    """
    if roots is None:
        roots = hermes_env_roots(uid, ancestor_uids, trust_root)
    try:
        text = read_confined_text(path, roots, max_bytes, uid, ancestor_uids, trust_root)
    except CredentialFileError as exc:
        print(f"hermes-usage: refusing credential file {path}: {exc}", file=sys.stderr)
        return False
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = re.sub(r"^export\s+", "", line)
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _ENV_NAME_RE.match(key):
            continue
        if "\x00" in value:  # os.environ rejects NUL — skip rather than crash
            continue
        if key in _ENV_DENYLIST:
            print(f"hermes-usage: ignoring {key} from the credential file",
                  file=sys.stderr)
            continue
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
        os.environ[key] = value.strip()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Hermes provider usage/balance as JSON.")
    parser.add_argument("--env", default=_expand_home("~/.hermes/.env"),
                        help="Path to the Hermes profile .env file with provider API "
                             "keys. Must live inside the Hermes profile directory "
                             "($HERMES_HOME — when it is inside your home — or "
                             "~/.hermes), or it is refused.")
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
