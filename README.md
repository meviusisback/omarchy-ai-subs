# AI Subs for Omarchy

![](infographic.png)

AI subscription usage and balance directly in the Omarchy bar — meter bars,
credit balances and live reset countdowns, for every provider you have
configured (API keys in `~/.hermes/.env` or Bitwarden Secrets Manager, native
config files, or OAuth tokens).

```text
OC · 5h 0% [──] (4h) · W 79% [━━──] (2d) · M 46% [━───] (16d)     (Data mode bar chip)
```

## Features

- **Meter bars per limit window** — OpenCode Go's rolling/weekly/monthly
  windows and prepaid credit bars, with color thresholds (accent past 70%,
  urgent red past 90%).
- **Live reset countdowns** — "resets in 2d 14h" under each window, ticking
  every second while the panel is open, and compact `(2d)` hints in the bar
  chip.
- **Two bar display modes** — a Sigma glyph or a compact one-liner for your
  default sub; switchable from the panel, persisted in `shell.json`.
- **Clean by default** — providers without keys never show up; no greyed-out
  placeholders.
- **Settings live in the panel** — Bar Icon/Data toggle and default-sub
  selector as segmented chips; no config files to hand-edit (but
  `omarchy bar set` works too).

## Supported providers

| Display | Provider | Metric | Key source |
|---------|----------|--------|------------------------|
| `OC` | OpenCode Go (+ Zen) | % used (5h / week / month) + resets | `OPENCODE_GO_API_KEY` |
| `OR` | OpenRouter | USD credits remaining | `OPENROUTER_API_KEY` |
| `CL` | Claude Code | % used per limit window + resets | none — reads Omarchy agent usage records |
| `CX` | Codex | % used per limit window + resets | none — reads Omarchy agent usage records |
| `CC` | Command Code | % used (5h / week / month) + USD balance remaining | `COMMANDCODE_API_KEY` |
| `DS` | DeepSeek | USD balance | `DEEPSEEK_API_KEY` (or `~/.deepseek/config.toml`) |
| `KI` | Kimi / Moonshot | USD balance | `KIMI_API_KEY` (or `~/.kimi-code/config.toml`) |
| `NV` | NovitaAI | USD balance | `NOVITA_API_KEY` |
| `Z` | ZAI / Zhipu | CNY balance | `ZAI_API_KEY` |
| `AB` | Alibaba / DashScope | USD balance | `DASHSCOPE_API_KEY` |
| `AR` | Arcee AI | USD balance | `ARCEE_API_KEY` |
| `CP` | GitHub Copilot | % used (chat / completions) | `GITHUB_TOKEN` if set, else the gh CLI config file (`~/.config/gh/hosts.yml`) or the editor OAuth token (opt-in) |
| `CU` | Cursor | USD plan spend | none — reads from Cursor local state DB (opt-in) |

Claude and Codex need no API keys: the widget reads the usage records that
Omarchy's own agent collectors write to
`~/.local/state/omarchy/agents/usage/`. Run those agents through Omarchy and
their limits appear automatically.

Every keyed row above can also come from Bitwarden Secrets Manager when it is
configured — see [Bitwarden Secrets Manager (optional)](#bitwarden-secrets-manager-optional);
when a key exists in both places, the `.env` value wins.

DeepSeek and Kimi fall back to their native config files (`~/.deepseek/config.toml`
and `~/.kimi-code/config.toml`) when the env var is not set.

Copilot and Cursor are **opt-in OAuth providers** (Tier 2): they read tokens
from the agent's own credential store and use them read-use-discard (never
cached, never logged). Activation is passive: the provider appears as soon as
the token already exists in the environment or credential store — nothing is
written and no opt-in flag is required.

> **Not supported:** Gemini (no public usage API — Google Cloud billing only)
> and OpenCode Zen credits (balance endpoint requested upstream in
> [anomalyco/opencode#10448](https://github.com/anomalyco/opencode/issues/10448);
> the shared Go key currently only exposes `/zen/go/v1/usage`).

## Install

```bash
omarchy plugin add https://github.com/meviusisback/omarchy-ai-subs.git --enable --yes
```

or interactively:

```bash
omarchy plugin add https://github.com/meviusisback/omarchy-ai-subs.git
```

Then add your API keys to `~/.hermes/.env`:

```sh
OPENCODE_GO_API_KEY=...
OPENROUTER_API_KEY=...
# etc.
```

Providers appear as soon as their key exists — no restart needed (the fetcher
refreshes every 15 minutes; right-click the widget or press `R` in the panel
to refresh immediately). Keys kept in Bitwarden Secrets Manager work too — see
[Bitwarden Secrets Manager (optional)](#bitwarden-secrets-manager-optional);
when a key exists in both places, the `.env` value wins.

## Update & Remove

```bash
# Update a git-managed plugin
omarchy plugin update meviusisback.ai-subs --yes

# Remove the plugin entirely
omarchy plugin remove meviusisback.ai-subs --yes
```


## Settings

Click the bar icon to open the panel:

- **Bar** — `Icon` (Sigma glyph) or `Data` (compact one-liner for the default
  sub, e.g. `OC · 5h 0% (4h) · W 79% (2d) · M 46% (16d)`).
- **Sub** — which provider Data mode shows (only configured ones are listed).

Equivalent CLI:

```bash
omarchy bar set meviusisback.ai-subs barDisplay Data
omarchy bar set meviusisback.ai-subs defaultSub openrouter
omarchy bar set meviusisback.ai-subs refreshIntervalSec 300
```

## How it works

A stdlib-only Python script (`fetch_usage.py`) queries each vendor's
usage/balance endpoint in parallel and prints one JSON document; the QML panel
(`Panel.qml`) renders it as meter bars and countdowns inside the Omarchy shell.

**Credential sources** — API keys are resolved in this order, each source only
filling what the previous ones did not supply:
1. Environment variables in the `--env` file (`~/.hermes/.env` by default)
2. Bitwarden Secrets Manager — **optional, off by default** (see below). Values
   it resolves are exported into the same environment, so downstream they
   behave exactly like source 1
3. Native config files (`~/.deepseek/config.toml`, `~/.kimi-code/config.toml`)

Providers that need **no API key** at all: OAuth tokens from agent credential
stores (Copilot, Cursor — read-use-discard, never cached) and Omarchy collector
records (`~/.local/state/omarchy/agents/usage/`) for Claude and Codex.

**Network egress**, stated exactly:
- the vendor usage/balance endpoints listed above, authenticated with your own key;
- `bws secret list` against Bitwarden's Secrets Manager API — **only** when
  `secrets.bitwarden` is enabled in `~/.hermes/config.yaml` *and* a key is still
  missing. With the integration off (the default) the widget makes zero extra
  requests and behaves exactly as it did without the feature;
- `date.nager.at` (public holidays, no credentials) — only when the optional
  DeepSeek peak-clock setting is on, which is off by default.

## Bitwarden Secrets Manager (optional)

For setups that keep provider keys out of files on disk. Enable it once:

```bash
hermes secrets bitwarden setup    # stores one bootstrap token (BWS_ACCESS_TOKEN) in ~/.hermes/.env
hermes secrets bitwarden status
```

After that the fetcher resolves whatever key the `.env` file does not have by
running `bws secret list <project>` with that bootstrap token. Guarantees:

- **`.env` values always win** — a key already in the file is never overridden.
- Nothing is written to disk: fetched values live in the fetcher's process
  environment only for the duration of one run, and the bootstrap token is
  passed to `bws` through the child's environment, never on its command line.
- Not configured → no `bws` process is spawned at all and no request is made.
- `server_url` must be `https://` without embedded credentials, `project_id`
  must be a UUID, and the token variable must be a Bitwarden-shaped name —
  anything else in the config is ignored with one line in the shell log.

## License

MIT — see [LICENSE](LICENSE).
