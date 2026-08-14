# pomo

[![CI](https://github.com/gopar/pomoism/actions/workflows/ci.yml/badge.svg)](https://github.com/gopar/pomoism/actions/workflows/ci.yml)

A pomodoro timer for people who live in the terminal and work across multiple
machines. Sessions sync to your home-base server so you can start a session on
one machine and see the countdown and get the overtime warning on another.

- **Multi-machine sync** — laptop, desktop, any machine. Start on one, see it
  everywhere.
- **Works offline** — the timer is local. Write the cache now, sync later.
- **Hooks, not built-ins** — every side effect (notifications, Focus mode,
  sounds) is an executable script you control. Language-agnostic.
- **Project tagging** — tag sessions to projects: `pomo 25 -p website`.
  Filter history by project, see everything you've worked on.
- **Zero dependencies** — Python 3.11+ stdlib only.

---

## Install

```sh
uv tool install https://github.com/gopar/pomoism.git
# Can also install via pip
```

This puts `pomo`, `pomo-agent`, and `pomo-server` on your `PATH`.

---

## Usage

### Start a session

```sh
pomo start 25                        # 25-minute pomodoro
pomo start 25 -p website             # ...tagged to a project
pomo start 25 -p website -n "login"  # ...with a name
pomo break 5                         # 5-minute break
pomo clear                           # stop + optionally start a break
```

### Check status

```sh
pomo status
# 🍅 18:22 [website] [fix login]

pomo status --json
# {"state": "pomodoro", "remaining": 1102, "display": "🍅 18:22 [website] [fix login]", ...}
```

### History

```sh
pomo history
# 2026-08-01  09:14 – 09:39  🍅  25:00  [website]
# 2026-08-01  09:42 – 10:12  🍅  30:09  [website] [fix login]
# 2026-08-01  10:15 – 10:20  ☕  05:00  [website]

pomo history --from 2026-08-01                    # from date to now
pomo history --from 2026-08-01 --to 2026-08-07    # date range
pomo history --state ended                        # only completed sessions
pomo history --project website --state pomodoro   # combine filters
pomo history --json                               # machine-readable
```

| Flag | Description |
|---|---|
| `--from DATE` | Start date (YYYY-MM-DD) |
| `--to DATE` | End date (YYYY-MM-DD) |
| `--state STATE` | Filter by session state (`pomodoro`, `overtime`, `break`, `break-overtime`, `ended`, `archived`) |
| `--json` | Output as JSON |
| `-p`, `--project` | Filter by project |

### Stats

```sh
pomo stats
# 2026-08-10
#   Sessions:    5
#   Focus time:  2h 05m
#
#   By project:
#   website      4 sessions   1h 40m
#   cli-tool     1 session      25m

pomo stats --from 2026-08-01 --to 2026-08-07   # date range
pomo stats --project website                     # filter by project
pomo stats --include-archived                    # include archived sessions
pomo stats --json                                # machine-readable
```

| Flag | Description |
|---|---|
| `--from DATE` | Start date (YYYY-MM-DD) |
| `--to DATE` | End date (YYYY-MM-DD) |
| `-p`, `--project` | Filter by project |
| `--include-archived` | Include archived sessions |
| `--json` | Output as JSON |

### Projects

```sh
pomo projects
# website
# backend
# cli-tool

pomo projects --json
# [{"project": "website"}, {"project": "backend"}, ...]
```

### Config

```sh
pomo config
# Config file: ~/.config/pomo/agent.toml (found)
#
# server_url = "http://127.0.0.1:8787"
# machine_name = "laptop"
# poll_interval = 5
# run_for_remote_sessions = false
#
# [hooks]
# enabled = true
# timeout = 10
# dir = ""

pomo config --json      # machine-readable (path, exists, effective config)
pomo config --init      # create ~/.config/pomo/agent.toml from the built-in sample if missing
```

### Full help

```
pomo --help
usage: pomo [-h] [--version]
            {start,break,clear,status,history,projects,stats,config,service} ...

Start, stop, and track pomodoro sessions.

positional arguments:
  {start,break,clear,status,history,projects,stats,config,service}
    start               Start a pomodoro for N minutes
    break               Start a break for N minutes
    clear               Stop current session, optionally start a break
    status              Show current session status
    history             Show session history
    projects            List all defined projects
    stats               Show session statistics
    config              Show configuration
    service             Manage pomo processes

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

---

## Setup

1. **Create the config file:**

   ```sh
   pomo config --init
   ```

   Writes `~/.config/pomo/agent.toml` from the built-in sample (defaults with
   explanatory comments).

   Edit `server_url` to point at your home-base machine. A Tailscale hostname
   works well. Set `POMO_TOKEN` on the server and all agents if you want
   bearer auth (disabled by default). Set `POMO_SERVER_URL` in your shell to
   pass it to the service at install time.

2. **Home-base machine — start the server:**

   ```sh
   # Foreground (terminal, for debugging):
   pomo service server

   # Background service:
   pomo service server install
   ```

   Docker (use *instead of* the service — run one, not both):

   ```sh
   docker compose up -d
   ```

3. **Every machine — start the agent:**

   ```sh
   # Foreground (terminal, for debugging):
   pomo service agent

   # Background service:
   pomo service agent install
   ```

4. **Check service status or tail logs:**

   ```sh
   pomo service agent status
   pomo service agent logs
   ```

Offline behavior: the CLI writes the local cache immediately and queues the
push; the agent syncs on reconnect (last-write-wins by timestamp).

You can also start the agent/server directly as foreground processes for
debugging.

---

## Hooks

Every side effect is a hook — the agent ships with zero built-in effects and
is OS-agnostic. Drop executable scripts into per-machine directories:

```
~/.config/pomo/hooks/<event>.d/*
```

Events: `pomodoro_start`, `break_start`, `pomodoro_overtime`,
`break_overtime`, `session_stop`. Scripts run in lexical filename order
(prefix with `10-`, `20-`, …).

```sh
mkdir -p ~/.config/pomo/hooks/pomodoro_start.d
cp hooks/examples/pomodoro_start.d/10-focus-on.sh \
   ~/.config/pomo/hooks/pomodoro_start.d/
chmod +x ~/.config/pomo/hooks/pomodoro_start.d/10-focus-on.sh
```

Examples included in `hooks/examples/`:

| Event              | Script                | What it does                  |
|---------------------|----------------------|-------------------------------|
| `pomodoro_start`   | `10-focus-on.sh`     | Enable macOS Focus / DnD      |
| `break_start`      | `10-focus-off.sh`    | Disable Focus                 |
| `break_start`      | `20-launch-emacs.sh` | Open Emacs (idle-time capture) |
| `pomodoro_overtime` | `10-announce.sh`     | `say` "Time's up" / alarm     |
| `break_overtime`   | `10-announce.sh`     | `say` "Break's over"          |
| `session_stop`     | `10-focus-off.sh`    | Disable Focus                 |

Each script receives context:

- **Env vars:** `POMO_EVENT`, `POMO_STATE`, `POMO_START_EPOCH`, `POMO_DURATION`,
  `POMO_MACHINE`, `POMO_ORIGIN_MACHINE`, `POMO_REMOTE` (`0`/`1`),
  `POMO_SESSION_ID`, `POMO_SESSION_PROJECT`.
- **Stdin:** the full session dict as JSON.

Hooks are best-effort: a failing, missing, or slow script (killed after
`hooks.timeout` seconds) never affects the timer or CLI. Tune via `[hooks]`
in `agent.toml`.

---

## Config & paths

| What           | Path / env                                  |
|----------------|----------------------------------------------|
| Config         | `~/.config/pomo/agent.toml`                 |
| Cache          | `~/.cache/pomo/` (session + offline outbox)  |
| Server DB      | `~/.local/share/pomo/pomo.db` (`POMO_DB_PATH`) |
| Server URL     | `POMO_SERVER_URL` (agent/CLI)                |
| Bearer token   | `POMO_TOKEN` (server + agents)               |
| Server port    | `POMO_PORT` (default 8787)                   |

### Config keys (`agent.toml`)

| Key                        | Default                | Notes                        |
|----------------------------|------------------------|------------------------------|
| `server_url`               | `http://127.0.0.1:8787` | Overridden by `POMO_SERVER_URL` |
| `machine_name`             | hostname               |                              |
| `poll_interval`            | 5                      | Seconds, minimum 5            |
| `run_for_remote_sessions`  | false                  | Fire hooks for sessions started on other machines |
| `hooks.enabled`            | true                   |                              |
| `hooks.timeout`            | 10                     | Seconds per hook script       |
| `hooks.dir`                | `~/.config/pomo/hooks` |                              |

---

## Server API

| Method | Path             | Description                               |
|--------|-----------------|--------------------------------------------|
| GET    | `/health`       | `{"ok": true}`                             |
| GET    | `/version`      | `{"version": "0.1.0"}`                     |
| GET    | `/current`      | Current session or `{"state": "idle"}`     |
| GET    | `/sessions`     | Sessions (optional `?project=`, `?from=`, `?to=`, `?state=`) |
| GET    | `/stats`        | Aggregated statistics (optional `?project=`, `?from=`, `?to=`, `?include_archived=`) |
| GET    | `/projects`     | All defined project names                   |
| POST   | `/sessions`     | Upsert a session (LWW; stale writes return `"applied": false`) |
| POST   | `/sessions/end` | End the current session                     |

Auth: optional bearer token via `POMO_TOKEN`.

---

## Architecture

Three small processes share `pomo/common.py`:

- **`pomo/server.py`** — HTTP/JSON source of truth backed by SQLite with append-only
  history. Conflicts resolve last-write-wins by timestamp. Runs on one
  home-base machine.
- **`pomo/agent.py`** — per-machine daemon. Polls the server, owns the local
  countdown→overtime timer, fires hooks, flushes the offline outbox.
- **`pomo/cli.py`** — the CLI. Writes the local cache immediately, pushes to the
  server (or queues to outbox if offline).

Sessions move through states: `pomodoro`/`break` → `overtime`/`break-overtime`
→ `ended`. Each session has a `kind` (set at creation: `pomodoro` or `break`)
that survives all transitions, so history always shows the right icon.

Because the timer and hooks are local, everything works with no network.

---

## Development

Tests are `pytest` (stdlib `unittest.mock` for patches):

```sh
pytest
```

Dev install includes ruff and ty:

```sh
uv sync --dev
ruff check . ; ruff format . ; ty check pomo
```

CI runs the test suite on Python 3.11–3.14, plus ruff lint/format, ty type
check, and a `compileall` syntax gate. See `AGENTS.md` for architecture
invariants.

---

## License

MIT
