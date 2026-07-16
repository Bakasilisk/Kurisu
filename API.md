# Kurisu Web API Specification

Reference for applications consuming the bot's read-only HTTP/JSON API (the `webapi` cog, `cogs/webapi.py`). The canonical consumer is the [kurisu-web](https://github.com/Bakasilisk/kurisu-web) dashboard, but any server-side application holding an API key can use it.

## Overview

- **Protocol:** HTTP/1.1, JSON responses (`application/json`). All endpoints are `GET`; there are no mutating endpoints — the API is strictly read-only.
- **Bind address:** `WEBAPI_HOST`:`WEBAPI_PORT` (defaults `127.0.0.1:8080`). The server binds localhost by default; public exposure is expected to happen via a TLS-terminating reverse proxy (e.g. an `api.` nginx server block).
- **Availability:** if the `WEBAPI_KEY` environment variable is unset, the server does not start at all. If the bot is running but has recorded no stats yet (`stats.db` missing), endpoints still respond — statistics simply come back as zeros / `null` / empty lists.

### Security model

This is a **trusted server-to-server API with no per-user authorization**. A valid key grants access to every guild the bot is in and every member's statistics. Consumers (like kurisu-web) must enforce their own user-facing scoping (e.g. "admins only see their own guilds") *before* calling this API. Never expose the API or its key directly to browsers.

### Authentication

Every request — including `/api/meta` — must carry the key in a header:

```
X-API-Key: <key>
```

The server accepts any key from the comma-separated `WEBAPI_KEY` set (multiple keys allow zero-downtime rotation and multiple consumers). Comparison is constant-time. A missing or wrong key yields:

```
401 {"error": "unauthorized"}
```

### Errors

Errors are JSON objects with a single `error` string:

| Status | Body | Meaning |
|---|---|---|
| `400` | `{"error": "invalid guild id"}` | `{gid}` is not an integer |
| `400` | `{"error": "invalid user id"}` | `{uid}` is not an integer |
| `401` | `{"error": "unauthorized"}` | missing/invalid `X-API-Key` |
| `404` | `{"error": "unknown guild"}` | the bot is not in guild `{gid}` (or it isn't cached) |
| `500` | `{"error": "internal error"}` | unhandled server error (details in the bot log) |

Note: requesting stats for a **user** the bot doesn't know is *not* an error — see `/members/{uid}` below.

## Conventions

- **Snowflake IDs are strings.** Discord IDs exceed 2^53 and would be corrupted by JSON consumers using IEEE-754 doubles (i.e. JavaScript). Path parameters (`{gid}`, `{uid}`) are the decimal snowflake.
- **Days and hours are UTC.** Dates appear as `"YYYY-MM-DD"` strings; hours are integers `0–23` in UTC.
- **`period` query parameter.** Endpoints that accept it take one of `week` (last 7 days), `month` (last 30 days), `year` (last 365 days), or `all`. The window is computed at day granularity: `day >= today_utc − N days`. An unrecognized value silently falls back to the endpoint's default (no error). The response echoes the period actually used.
- **`limit` query parameter.** The ranked-list endpoints (`/top`, `/channels`, `/voice`, `/quietest`) accept an optional `limit` — a positive integer capping the number of `entries` returned (applied after sorting, so it's always the top/bottom N). When absent, non-numeric, or `<= 0`, the full list is returned (no error), which can be as long as the guild's member/channel count — pass a `limit` unless you really need everything. There is no offset/pagination.
- **No rate limiting.** Be a considerate consumer; every request runs SQL against the bot's stats database.

### Shared objects

**User** — resolved from the bot's live member cache:

```json
{"id": "123456789012345678", "name": "Display Name", "avatar": "https://cdn.discordapp.com/..."}
```

If the user isn't in the cache (left the guild, or the cache is cold), `name` is `"Unknown"` and `avatar` is `null`. `name` is the guild display name (nickname if set).

**Channel:**

```json
{"id": "123456789012345678", "name": "general"}
```

If unresolvable, `name` is `"unknown-channel"`. The name has no leading `#`.

**Guild:**

```json
{"id": "123456789012345678", "name": "My Server", "icon": "https://cdn.discordapp.com/..."}
```

`icon` is `null` if the guild has no icon.

---

## Endpoints

### GET `/api/meta`

Bot-level metadata, intended for the consumer's "is this user the bot owner?" check.

```json
{"owner_id": "123456789012345678", "guild_count": 3}
```

- `owner_id` — string snowflake of the bot application's owner, or `null` if it can't be resolved.
- `guild_count` — number of guilds the bot is currently in.

### GET `/api/guilds`

All guilds the bot is in, as an array of Guild objects:

```json
[{"id": "…", "name": "…", "icon": null}, …]
```

### GET `/api/guilds/{gid}/overview`

All-time and recent-trend summary for one guild.

```json
{
  "guild": {"id": "…", "name": "…", "icon": "…"},
  "total_messages": 41230,
  "total_words": 310022,
  "first_day": "2025-11-02",
  "active_members_30d": 41,
  "avg_day": 162.3,
  "reactions": 5120,
  "voice_seconds": 884211,
  "trend": {"recent": 4801, "prior": 4302, "pct": 11.6, "text": "up 11.6% vs prior 30d"}
}
```

- `total_messages` / `total_words` — all-time totals across all tracked channels.
- `first_day` — earliest day with recorded data, or `null` if the guild has no data.
- `active_members_30d` — distinct users who sent at least one message in the last 30 days.
- `avg_day` — float: `total_messages / days elapsed since first_day` (inclusive; at least 1 day).
- `reactions` — all-time reactions given by members.
- `voice_seconds` — all-time voice-channel time, summed over members, in seconds.
- `trend` — last 30 days (`recent`) vs the 30 days before that (`prior`). `pct` is a float percentage change, or `null` when `prior` is 0 (no baseline). `text` is a human-readable summary of the same numbers.

For a poster leaderboard, use `/top` — earlier versions of this endpoint included a `top_posters` list; it has been removed.

### GET `/api/guilds/{gid}/top`

Message-count leaderboard.

Query: `period` (default `all`), `limit`.

```json
{"period": "all", "entries": [{"user": {…}, "count": 9241}, …]}
```

`entries` is sorted by `count` descending and covers every user with at least one message in the window (up to `limit`).

### GET `/api/guilds/{gid}/channels`

Per-channel message counts.

Query: `period` (default `all`), `limit`.

```json
{"period": "all", "entries": [{"channel": {"id": "…", "name": "general"}, "count": 15044}, …]}
```

Sorted by `count` descending. May include channels that have since been deleted (`name: "unknown-channel"`).

### GET `/api/guilds/{gid}/activity`

Hour-of-day × day-of-week activity heatmap data.

Query: `period` (default `month`).

```json
{
  "period": "month",
  "grid": [[0, 3, …, 12], …],
  "weekday_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
  "hour_totals": [51, 40, …],
  "weekday_totals": [812, 790, …],
  "peak_hour": 20,
  "peak_weekday": 5,
  "total": 4801
}
```

- `grid` — 7 rows × 24 columns of message counts; row index is weekday (0 = Monday, matching `weekday_labels`), column index is UTC hour (0–23).
- `hour_totals` — 24 ints, `grid` summed over weekdays.
- `weekday_totals` — 7 ints, `grid` summed over hours.
- `peak_hour` / `peak_weekday` — indices of the busiest hour/weekday. Both are `0` when `total` is 0 (not `null`) — check `total` before treating them as meaningful.
- `total` — sum of the whole grid.

### GET `/api/guilds/{gid}/voice`

Voice-time leaderboard.

Query: `period` (default `all`), `limit`.

```json
{"period": "all", "entries": [{"user": {…}, "seconds": 88421}, …]}
```

Sorted by `seconds` descending.

### GET `/api/guilds/{gid}/growth`

Membership change plus message volume for the window.

Query: `period` (default `month`).

```json
{"period": "month", "joins": 14, "leaves": 6, "net": 8, "messages": 4801}
```

`net` is always `joins − leaves`.

### GET `/api/guilds/{gid}/members/{uid}`

Per-member statistics (all-time).

An unknown or departed member is **not** a 404: statistics are looked up regardless, so a user with no recorded data returns zeros with `user.name = "Unknown"`. Treat `total_messages == 0` together with `server_rank == null` as "no data".

```json
{
  "user": {"id": "…", "name": "…", "avatar": "…"},
  "total_messages": 9241,
  "total_words": 71204,
  "active_days": 210,
  "first_day": "2025-11-02",
  "server_rank": 1,
  "pct_of_server": 22.4,
  "words_per_msg": 7.7,
  "busiest_hour": 20,
  "voice_seconds": 88421,
  "reactions_given": 1204,
  "reactions_received": 990,
  "top_channels": [{"channel": {…}, "count": 5120}, …]
}
```

- `active_days` — number of distinct days with at least one message.
- `first_day` — the member's earliest recorded day, or `null` if none.
- `server_rank` — 1-based rank by all-time message count among users with recorded messages, or `null` if the member has none.
- `pct_of_server` — float, this member's share of the guild's all-time messages (0–100); `0.0` when the guild has no messages.
- `words_per_msg` — float; `0.0` when the member has no messages.
- `busiest_hour` — UTC hour (0–23) with the most messages, or `null` if the member has none.
- `top_channels` — every channel the member has posted in, sorted by count descending.

### GET `/api/guilds/{gid}/quietest`

Least-active members over a fixed 30-day window (no `period` parameter).

Query: `limit`.

```json
{"entries": [{"user": {…}, "count": 0}, …]}
```

- Covers every **non-bot member currently in the guild's cache**, including members with zero messages — unlike the leaderboards, absence of data is the point here.
- Sorted by `count` ascending (quietest first), truncated to `limit` after sorting. Without a `limit` the full member list is returned — for a large guild, always pass one.

### GET `/api/guilds/{gid}/leveling`

**Tier:** harmless

XP leaderboard from the `leveling` cog.

Query: `limit`.

```json
{"entries": [{"user": {…}, "xp": 4820, "level": 12}, …]}
```

- Sourced from `xp.json` (not `stats.db`) — this is the leveling cog's own cumulative XP counter, independent of message-count stats.
- `entries` is sorted by `xp` descending, covering every user with an entry in `xp.json` (up to `limit`). No `period` parameter — XP is cumulative, not windowed.
- `level` is derived from the same level curve the bot itself uses (`total_xp_for_level`/`level_from_xp`, duplicated in `cogs/webapi.py` to avoid importing the `leveling` cog).
- Eventual consistency: `xp.json` is flushed to disk by the leveling cog roughly every 30 seconds, so values here can trail live activity by up to one flush interval.

### GET `/api/guilds/{gid}/economy`

**Tier:** harmless

Bits-balance leaderboard from the `economy` cog.

Query: `limit`.

```json
{"entries": [{"user": {…}, "bits": 3150}, …]}
```

- Sourced from `economy.json` (not `stats.db`).
- `entries` is sorted by `bits` (balance) descending, covering every user with an entry in `economy.json` (up to `limit`). No `period` parameter.
- Economy balances save on every change, so this data is fresh (no flush-interval lag, unlike `/leveling`).

---

## Data notes

- Statistics come from the bot's `stats.db` (populated by the `stats` cog's live listeners and optional `stats backfill`). They only cover activity since collection began (or as far back as a backfill ran) — not the guild's full Discord history.
- The API reads the database via its own read-only connection concurrently with the stats writer (WAL), so values can trail live Discord activity by up to one flush interval.
- If a query fails or the database doesn't exist yet, affected statistics come back as zeros / `null` / empty lists rather than an error.

## Configuration reference (server operator)

Set in the bot's `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `WEBAPI_KEY` | *(unset — API disabled)* | Comma-separated set of accepted API keys |
| `WEBAPI_HOST` | `127.0.0.1` | Bind address |
| `WEBAPI_PORT` | `8080` | Bind port |
| `STATS_DB_PATH` | `<repo>/stats.db` | Stats database location (shared with the stats cog) |
