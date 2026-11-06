<div align="center">

# AI Subs

**AI subscription usage and balance in the Omarchy bar — meter bars, credit balances and live reset countdowns for every provider you have configured.**

[![License: MIT](https://img.shields.io/github/license/meviusisback/omarchy-ai-subs)](LICENSE)
![Linux](https://img.shields.io/badge/linux-supported-1793d1)

![AI Subs panel: provider usage meters, balances and reset countdowns](preview.png)
![AI Subs feature overview](infographic.png)

</div>

## Why

AI spend is scattered across a dozen subscriptions, each with its own dashboard, window and reset clock. AI Subs collapses all of it into a single bar chip: one glance tells you which subscription is hot, which is exhausted, and when each one resets — with a panel behind it for the details and the settings.

## Features

- **Meter bars per limit window** — each subscription gets its own meter(s) per limit window (rolling/weekly/monthly for OpenCode Go, 5h and 7-day for Claude and Codex, credit and balance bars elsewhere), coloured by theme: the accent past 70%, the urgent tone past 90%.
- **Balance and credit accounting** — prepaid balances and credit/usage meters read from each vendor's own API (OpenRouter, Kimi, DeepSeek, NovitaAI, ZAI, Alibaba, Arcee, Command Code). The fetcher speaks HTTP only: it never shells out to a vendor CLI.
- **Live reset countdowns** — each window shows the time to its reset (`(2d)`, `(2h)`, `(16m)`), taken from the `resetsAt` the vendor returns; a window whose vendor exposes no reset time simply shows none.
- **Two bar display modes** — `Data` (default): a one-liner for the selected subscription (`OC · 5h 0% (4h) · W 79% (2d) · M 46% (16d)`); `Icon`: the `Σ` glyph alone. Hovering either form shows the same short tooltip — `AI Subs`, plus `· DS peak` / `· DS off-peak` when the pricing clock is on; the meters and balances live in the click-through panel.
- **DeepSeek peak/off-peak indicator** — optional clock next to the Sub box: **PEAK** during Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, **OFF** the other 17 weekday hours, weekends and Chinese public holidays, **OFF BY DEFAULT** — no key required, plain UTC windows + cached public-holiday list.
- **Settings live in the panel** — Bar: `Icon`/`Data`; Sub: selector that lists only the subscriptions actually configured; Clock: `Deepseek Off`/`On`. Written straight into the bar entry (`updateEntryInline`), so the change applies without a restart.
- **Clean by default** — providers with no credentials never show as empty rows; Claude Code and Codex read local session data and need no key at all.
- **Bitwarden Secrets Manager (optional)** — keys missing from the `.env` file can come from a Bitwarden project instead; `.env` values always win, nothing is written to disk, and with the integration off (the default) no `bws` process is spawned and no extra request is made.

Not supported, deliberately: Gemini web subscriptions (no public usage API — Google Cloud billing only), and OpenCode Zen credits (balance endpoint requested upstream in [anomalyco/opencode#10448](https://github.com/anomalyco/opencode/issues/10448); the shared Go key currently only exposes `/zen/go/v1/usage`).

## Install

```bash
omarchy plugin add https://github.com/meviusisback/omarchy-ai-subs.git --enable --yes
```

or interactively (you pick the flags):

```bash
omarchy plugin add https://github.com/meviusisback/omarchy-ai-subs.git
```

Then add the keys for the providers you use to `~/.hermes/.env`, one per line:

```bash
OPENCODE_GO_API_KEY=...
OPENROUTER_API_KEY=sk-or-...
```

Claude Code and Codex need no key: they read their own local session data.

Providers appear in the widget as soon as their key exists — no restart needed (the fetcher refreshes every 15 minutes; right-click the bar icon or press `R` for an immediate refresh). Keys kept in Bitwarden Secrets Manager work too — see [Bitwarden Secrets Manager (optional)](#bitwarden-secrets-manager-optional); when a key exists in both places, the `.env` value wins.

## Usage

- Click the bar icon to open the panel.
- **Bar** box: switch between `Icon` and `Data` display.
- **Sub** box: pick which subscription drives the bar chip (lists only the subscriptions actually configured).
- **Clock** box: `Deepseek Off` / `Deepseek On` for the peak/off-peak indicator.
- `R` refreshes; right-click the bar icon also refreshes.

Same settings from the terminal — the shell's bar-widget IPC:

```bash
omarchy-shell shell setBarWidget meviusisback.ai-subs barDisplay '"Data"' '{}'
omarchy-shell shell setBarWidget meviusisback.ai-subs defaultSub '"claude"' '{}'
omarchy-shell shell setBarWidget meviusisback.ai-subs dsPeakClock 'true' '{}'
```

The value is passed as JSON (`'"Data"'` for a string, `'true'` for a boolean).

## Requirements

- Omarchy with the Quickshell shell
- `python3` (the fetcher uses the standard library only)
- Internet access (vendor usage/balance APIs; the Nager.Date public-holiday API only while the pricing clock is on; the Bitwarden Secrets Manager API only while that integration is on)
- API keys in `~/.hermes/.env` (or the native config each provider reuses, or Bitwarden Secrets Manager — see below)
- GitHub Copilot token in `~/.config/github-copilot/apps.json`, or `GITHUB_TOKEN` / `~/.config/gh/hosts.yml` — Copilot usage only, picked up automatically when one of them exists
- Cursor local state (`~/.cursor/state.vscdb`) — Cursor usage only, opened read-only through SQLite when that file exists

## Configuration

Settings (panel dropdowns or the shell's `setBarWidget` IPC), with defaults:

| Key | Default | Options |
|---|---|---|
| `refreshIntervalSec` | `900` | seconds, 30–3600 |
| `hermesEnvFile` | `~/.hermes/.env` | `KEY=value` lines |
| `barDisplay` | `Data` | `Icon`, `Data` |
| `dsPeakClock` | `false` | `true` / `false` |
| `defaultSub` | `opencode` | `opencode`, `openrouter`, `claude`, `codex`, `deepseek`, `kimi`, `novita`, `zai`, `alibaba`, `arcee`, `commandcode`, `copilot`, `cursor` |

Provider → credential mapping (the `hermesEnvFile` keys are loaded into the environment first and override same-named exported variables, then Bitwarden fills what is still missing when enabled, then the provider's own file is used when no key is set):

| Provider | Key (env file) | Native fallback |
|---|---|---|
| opencode | `OPENCODE_GO_API_KEY`, `OPENCODE_ZEN_API_KEY` | — |
| openrouter | `OPENROUTER_API_KEY` | — |
| claude | — (local session data, no key) | `~/.local/state/omarchy/agents/usage/claude.json` (the same collector record the built-in Agents widget uses) |
| codex | — (local session data, no key) | `~/.local/state/omarchy/agents/usage/codex.json` |
| deepseek | `DEEPSEEK_API_KEY` | `~/.deepseek/config.toml` |
| kimi | `KIMI_API_KEY`, `MOONSHOT_API_KEY` | `~/.kimi-code/config.toml` |
| novita | `NOVITA_API_KEY` | — |
| zai | `ZAI_API_KEY`, `GLM_API_KEY` | — |
| alibaba | `DASHSCOPE_API_KEY` | — |
| arcee | `ARCEE_API_KEY` | — |
| commandcode | `COMMANDCODE_API_KEY` | — |
| copilot | — (OAuth token) | `~/.config/github-copilot/apps.json`, `GITHUB_TOKEN`, or `~/.config/gh/hosts.yml` |
| cursor | — (OAuth token) | `~/.cursor/state.vscdb`, opened read-only |

Key files stay locked down:

- The credential file must be a regular, single-linked file owned by you, with no group/other permission bits (`0600`, `0400` or any stricter mode), inside the allowed roots (the Hermes profile directory or `~/.hermes`). It is opened with `O_NOFOLLOW` and re-validated on the descriptor itself — anything else is refused and the keys that file would have supplied are simply absent.
- Explicit refusals, written to the shell log (`stderr` of the fetcher); the widget keeps rendering and the providers it would have supplied simply report no key: a symlink, more than one hard link, a different owner, group/other-readable bits, a path outside the allowed roots, or a file over 256 KiB.
- You own the chmod: recovery is `chmod 600 ~/.hermes/.env`, never an automatic fix.
- Provider failures reach the panel only as a short code (`http-401`, `network-error`, `unexpected-response`), never as the vendor's raw body, so nothing a vendor echoes back can carry a key into the UI.
- No secret ever reaches a process argument list: the fetcher is invoked with a fixed argv (the interpreter, `fetch_usage.py`, `--env` and the configured path, plus `--no-holidays` when the pricing clock is off), and keys travel only in HTTP `Authorization` headers built in memory. The bootstrap token reaches `bws` through the child's environment, never on its command line.

## Bitwarden Secrets Manager (optional)

For setups that keep provider keys out of files on disk. Enable it once:

```bash
hermes secrets bitwarden setup    # stores one bootstrap token (BWS_ACCESS_TOKEN) in ~/.hermes/.env
hermes secrets bitwarden status
```

After that the fetcher resolves whatever key the `.env` file does not have by running `bws secret list <project>` with that bootstrap token. Guarantees:

- **`.env` values always win** — a key already in the file is never overridden.
- Nothing is written to disk: fetched values live in the fetcher's process environment only for the duration of one run, and the bootstrap token is passed to `bws` through the child's environment, never on its command line.
- Not configured → no `bws` process is spawned at all and no request is made.
- `server_url` must be `https://` without embedded credentials, `project_id` must be a UUID, and the token variable must be a Bitwarden-shaped name — anything else in the config is ignored with one line in the shell log.
- Only the integrity-checked Hermes-pinned `bws` is ever executed (trusted directory, no symlinks, owned file without group/other write bits); the session `PATH` is never consulted.

## How it works

`fetch_usage.py` queries each vendor's own usage/balance endpoint in parallel and emits one JSON document; `Panel.qml` renders the meters, balances and countdowns from it. Credential sources, in order: the keys in the Hermes `.env` you point at with `hermesEnvFile` (loaded into the environment, so they win over same-named exported variables) → Bitwarden Secrets Manager when enabled (fills only what is still missing; off by default, zero extra requests when off) → provider-native files (`~/.deepseek/config.toml`, `~/.kimi-code/config.toml`, the Copilot token file or `GITHUB_TOKEN` / `~/.config/gh/hosts.yml`, Cursor's local SQLite state). No API keys, balances or usage data ever leave your machine except to the vendors' own API endpoints, the Bitwarden Secrets Manager API when that integration is on (and Nager.Date for the holiday list when the pricing clock is on); redirects are followed only within the same origin, so a key is never forwarded to another host or downgraded to plaintext.

Verify with `python3 -m unittest discover -s tests` — 83 tests, hermetic.

## Removal

```bash
omarchy plugin update meviusisback.ai-subs --yes   # update
omarchy plugin remove meviusisback.ai-subs --yes   # remove
```

## License

MIT — see [LICENSE](LICENSE).
