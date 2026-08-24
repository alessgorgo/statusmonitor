# StatusMonitor

A Red-DiscordBot cog that keeps **one** embed in a channel of your choice and rewrites it
every minute with the live status of the services you added, each with an uptime-style
history bar underneath its name.

```
Fleeq Service Status
🔴 1 of 3 services down
Last check: 12 seconds ago

🟢  Fleeq API
🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟨🟩🟩
`100.0%` uptime • Operational • 128ms

🔴  Postgres
🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟥🟥🟥🟥🟥
` 75.0%` uptime • Down • Timed out

Checked every 60s • 3 service(s)
```

Each square is one past check, oldest on the left, newest on the right:
🟩 up · 🟨 degraded (slow) · 🟥 down · ⬜ no data yet.

## Install

The cog lives in `statusmonitor/` next to this file. From Discord:

```
[p]addpath /Users/aleks/Desktop/Fleeq/bot
[p]load statusmonitor
```

(`[p]` is your prefix.) `[p]reload statusmonitor` picks up later edits.

## Setup

```
[p]statusmon channel #status
[p]statusmon add "Fleeq API" https://api.fleeq.io/health
[p]statusmon add Postgres db.fleeq.io:5432
[p]statusmon roles @Ops
[p]statusmon color #00b894
```

The panel is posted on the first check and edited in place from then on. Quote names
that contain spaces. Targets are either a link (`https://…`) or a `host:port` pair,
which is checked with a plain TCP connection.

## Commands

All commands live under `[p]statusmon` (alias `[p]stm`) and need admin or Manage Server.

| Command | What it does |
| --- | --- |
| `add <name> <target>` | Monitor a link or a `host:port` service (max 20) |
| `remove <name>` | Stop monitoring it |
| `list` | Table of services, targets and current state |
| `channel <#channel>` | Where the panel lives (moving it posts a new panel) |
| `roles [@role…]` | Roles mentioned on the panel; no argument clears them |
| `color <#hex>` | Embed colour — accepts `#00b894`, `0x00b894` or `blurple` |
| `title <text>` | Panel title |
| `history <5-40>` | How many past checks the bar shows (default 20) |
| `interval <60-3600>` | Seconds between checks (default 60) |
| `timeout <1-30>` | Seconds before a check counts as down (default 10) |
| `slow <ms>` | Latency above which a service is degraded/yellow (default 2000) |
| `expect <name> [codes…]` | HTTP codes that count as up; empty = any 2xx/3xx |
| `alerts <on/off>` | Extra ping message when a service changes state (default on) |
| `links <on/off>` | Show each service's link on the panel (default off) |
| `post` | Replace the panel with a fresh message |
| `refresh` | Check everything right now |
| `clearhistory` | Wipe the recorded history |
| `settings` | Show the current configuration |

## About the role ping

Discord only notifies people when a message is **sent**, not when it is edited. The
panel is a single edited message, so the roles in it ping once — when the panel is first
posted. So that you actually get notified when something goes down or comes back,
state-change alerts are **on by default**: a short separate message is posted next to the
panel whenever a service flips up ↔ down, mentioning the roles you set. The panel itself
stays the one always-current embed. Turn the alerts off with `[p]statusmon alerts off`
if you only want the silent panel.

## Notes

- Settings and history are per-server, stored in Red's Config.
- The bot needs Send Messages and Embed Links in the status channel.
- If someone deletes the panel, the next check posts a new one automatically.
- Only 2xx/3xx responses count as up unless you set `expect` for that service.
- A check that times out, is refused, or fails TLS verification counts as down.
