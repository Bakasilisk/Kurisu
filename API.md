# Kurisu Web API Specification

Reference for applications consuming the bot's read-only HTTP/JSON API (the `webapi` cog, `cogs/webapi.py`). The canonical consumer is the [kurisu-web](https://github.com/Bakasilisk/kurisu-web) dashboard, but any server-side application holding an API key can use it.

## Overview

- **Protocol:** HTTP/1.1, JSON responses (`application/json`). All endpoints are `GET`; there are no mutating endpoints — the API is strictly read-only.
- **Bind address:** `WEBAPI_HOST`:`WEBAPI_PORT` (defaults `127.0.0.1:8080`). The server binds localhost by default; public exposure is expected to happen via a TLS-terminating reverse proxy (e.g. an `api.` nginx server block).
- **Availability:** if the `WEBAPI_KEY` environment variable is unset, the server does not start at all. If the bot is running but has recorded no stats yet (`stats.db` missing), endpoints still respond — statistics simply come back as zeros / `null` / empty lists.

### Security model

This is a **trusted server-to-server API with no per-user authorization**. A valid key grants access to every guild the bot is in and every member's statistics. Consumers (like kurisu-web) must enforce their own user-facing scoping (e.g. "admins only see their own guilds") *before* calling this API. Never expose the API or its key directly to browsers.

### Sensitivity tiers

Every endpoint carries a documented sensitivity tier, `harmless`, `spicy`, or `self`, to support a **future** per-user access layer: any member would be able to read `harmless` data (their own and others' chat activity and gamification stats), `spicy` data (moderation actions, security posture, surveillance state, server setup/config) stays mod-only, and `self` data is visible only to the specific user it belongs to.

`self`-tier data isn't member-readable (not `harmless` — it's not meant for anyone-about-anyone) and isn't mod-readable (not `spicy` — a mod has no special claim to another user's personal data). A consumer must derive `{uid}` exclusively from its own authenticated session (exactly as kurisu-web's `/me` flow does today), **never** from user-supplied input — there is no guild-membership or moderator check that could substitute for that, since these endpoints aren't guild-scoped at all.

**Today, this tier is documentation and response-shape structure only** — it is *not yet enforced* by the API itself. Enforcement currently happens frontend-side (kurisu-web's own guild-scoping, and — for `self` — its own-session `{uid}` derivation) or is expected to happen server-to-server (a trusted consumer decides who sees what); `cogs/webapi.py` does not itself check a per-user role before answering a request. Treat the tier as a contract for how a future auth layer will gate these endpoints, and as a hard rule for this cog's own design: **a spicy field must never be nested inside a harmless endpoint's response** — e.g. warnings are deliberately not part of `/members/{uid}`, they live solely on the spicy `/warnings` endpoint, so that a future harmless-only auth scope can safely expose `/members/{uid}` wholesale without a field-level allowlist. The same isolation applies to `self`: a `self`-tier field must never be nested inside a `harmless` or `spicy` response either.

| Endpoint | Tier |
|---|---|
| `/meta`, `/guilds`, `/overview`, `/growth`, `/top`, `/channels`, `/voice`, `/leveling`, `/economy`, `/members/{uid}` | harmless |
| `/activity`, `/quietest`, `/warnings`, `/security`, `/palantir`, `/verification`, `/moderation`, `/features` | spicy |
| `/users/{uid}/reminders` | self |

Note: `/activity` is tiered **spicy** here by deliberate API-side operator choice, even though the bot's own `.stats activity` command is open to every member in the Discord UI. The two don't have to match — the API's tiering is a separate, intentionally more conservative decision (fine-grained hour×weekday activity patterns are treated as more sensitive in aggregate/API form than a one-off Discord command reply).

### Data sources / cog coverage

Which cogs' data is surfaced through this API, and which are deliberately left out:

| Cog | Surfaced | Notes |
|---|---|---|
| stats | yes (existing) | `overview`/`top`/`channels`/`activity`/`voice`/`growth`/`members/{uid}`/`quietest` |
| leveling | yes | `/leveling` + member enrichment |
| economy | yes | `/economy` + member enrichment |
| moderation | yes (spicy) | `/warnings` (warnings) + `/moderation` (mod-log channel + locked-channel list; restoration snapshots never exposed) |
| cerberus | yes (spicy) | `/security` |
| palantir | config only (spicy) | `/palantir` — **surveillance cache/content never exposed** |
| verification | config only (spicy) | `/verification` |
| reminders | yes (self) | `/users/{uid}/reminders` — a user's own pending reminders only |
| anilist, triggers, captions, aidetect, trace | no | stateless — nothing persisted |
| management | config only (spicy) | `/features` — per-guild `.feature` toggle state, one entry per toggleable cog; global disable state also surfaced |
| help, webapi | no | infra cogs — nothing persisted worth surfacing |

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

Note: the `/users/{uid}/...` endpoints (currently just `/users/{uid}/reminders`) aren't guild-scoped, so the `404 unknown guild` row never applies to them — an unknown `{uid}` on those endpoints isn't an error either (see `/users/{uid}/reminders` below), only a malformed `{uid}` is.

## Conventions

- **Snowflake IDs are strings.** Discord IDs exceed 2^53 and would be corrupted by JSON consumers using IEEE-754 doubles (i.e. JavaScript). Path parameters (`{gid}`, `{uid}`) are the decimal snowflake.
- **Days and hours are UTC.** Dates appear as `"YYYY-MM-DD"` strings; hours are integers `0–23` in UTC.
- **`period` query parameter.** Endpoints that accept it take one of `week` (last 7 days), `month` (last 30 days), `year` (last 365 days), or `all`. The window is computed at day granularity: `day >= today_utc − N days`. An unrecognized value silently falls back to the endpoint's default (no error). The response echoes the period actually used.
- **`limit` query parameter.** The ranked-list endpoints (`/top`, `/channels`, `/voice`, `/quietest`, `/leveling`, `/economy`, `/warnings`) accept an optional `limit` — a positive integer capping the number of `entries` returned (applied after sorting, so it's always the top/bottom N). When absent, non-numeric, or `<= 0`, the full list is returned (no error), which can be as long as the guild's member/channel count — pass a `limit` unless you really need everything. There is no offset/pagination. `/moderation`, `/features`, and `/users/{uid}/reminders` take no `limit` param at all — each returns an inherently bounded list (locked channels, toggleable cogs, a per-user reminder cap), not a leaderboard.
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

**Tier:** harmless

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
  "top_channels": [{"channel": {…}, "count": 5120}, …],
  "leveling": {"xp": 4820, "level": 12, "rank": 3},
  "economy": {"bits": 3150, "rank": 7, "streak": 4}
}
```

- `active_days` — number of distinct days with at least one message.
- `first_day` — the member's earliest recorded day, or `null` if none.
- `server_rank` — 1-based rank by all-time message count among users with recorded messages, or `null` if the member has none.
- `pct_of_server` — float, this member's share of the guild's all-time messages (0–100); `0.0` when the guild has no messages.
- `words_per_msg` — float; `0.0` when the member has no messages.
- `busiest_hour` — UTC hour (0–23) with the most messages, or `null` if the member has none.
- `top_channels` — every channel the member has posted in, sorted by count descending.
- `leveling` — `xp` (cumulative, from `xp.json`, defaults to `0`), `level` (derived via `level_from_xp`), and `rank` (1-based rank by `xp` among users with an `xp.json` entry, or `null` if the member has none).
- `economy` — `bits` (balance, from `economy.json`, defaults to `0`) and `rank` (1-based rank by balance, or `null` if the member has no entry).
- `streak` — consecutive-day payday streak from the economy cog; `0` for accounts that predate the streak mechanic (backfilled lazily on the next payday) or whose streak was reset.
- **Note:** warnings are deliberately **not** included here. Warnings are spicy/mod-tier data; they live solely on `GET /api/guilds/{gid}/warnings` so this endpoint stays safely member-readable.

### GET `/api/guilds/{gid}/quietest`

**Tier:** spicy

Least-active members over a fixed 30-day window (no `period` parameter). Tiered **spicy** to match the bot itself: the `stats` cog gates `.stats quietest` behind Manage Server (surfacing a "who's least active" call-out list is a moderation-adjacent view), so the API keeps it mod-tier for consistency.

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
{"entries": [{"user": {…}, "bits": 3150, "streak": 4}, …]}
```

- Sourced from `economy.json` (not `stats.db`).
- `entries` is sorted by `bits` (balance) descending, covering every user with an entry in `economy.json` (up to `limit`). No `period` parameter.
- Economy balances save on every change, so this data is fresh (no flush-interval lag, unlike `/leveling`).
- `streak` — consecutive-day payday streak from the economy cog; `0` for accounts that predate the streak mechanic (backfilled lazily on the next payday) or whose streak was reset.

### GET `/api/guilds/{gid}/warnings`

**Tier:** spicy

Moderation warnings from the `warnings` cog, one entry per warned user.

Query: `limit`.

```json
{
  "entries": [
    {
      "user": {"id": "…", "name": "…", "avatar": "…"},
      "count": 2,
      "warnings": [
        {
          "reason": "spamming in #general",
          "moderator": {"id": "…", "name": "ModName", "avatar": "…"},
          "timestamp": "2026-06-01T14:22:00+00:00"
        },
        {"reason": "…", "moderator": {…}, "timestamp": "…"}
      ]
    }
  ]
}
```

- Sourced from `warnings.json` (not `stats.db`).
- `entries` is sorted by `count` (number of warnings) descending, covering every user with at least one warning (up to `limit`). No `period` parameter.
- `moderator` is resolved from the bot's live member cache the same way as `user`. If the moderator has since left the guild (or the cache is cold), it comes back as `{"id": "…", "name": "Unknown", "avatar": null}` rather than a literal `null`. `moderator` is only a literal `null` in the rare case the stored warning itself has no `moderator_id`.
- This endpoint is **spicy**: unlike `/members/{uid}`, its data is mod-tier and must not be exposed to ordinary members by a consumer.

### GET `/api/guilds/{gid}/security`

**Tier:** spicy

Cerberus (raid/spam defense) configuration and live lockdown status.

```json
{
  "mode": "shadow",
  "log_channel": {"id": "…", "name": "mod-log"},
  "exempt_roles": 2,
  "exempt_users": 1,
  "protected_roles": 1,
  "lockdown": {
    "active": false,
    "started_at": null,
    "expires_at": null,
    "remaining_seconds": 0,
    "stay_locked": false
  }
}
```

- Sourced from `cerberus.json` (not `stats.db`).
- `mode` — `"shadow"` (detect + alert only) or `"active"` (also takes action); defaults to `"shadow"` if unconfigured.
- `log_channel` — the configured Cerberus alert channel, or `null` if unset.
- `exempt_roles` / `exempt_users` / `protected_roles` — **counts only**, not the actual role/user lists — a consumer that needs the specifics is expected to be a mod using the bot's own `.cerberus exempt list` / `.cerberus protectedrole list` commands, not this API.
- `lockdown.active` — whether a guild-wide lockdown is currently in effect.
- `lockdown.remaining_seconds` — seconds until auto-lift, `0` if not active or if `stay_locked`.
- `lockdown.stay_locked` — `true` when the lockdown has no `expires_at` (a repeat-trigger lockdown that holds until a mod runs `.cerberus unlock`), matching the same "stay locked" semantics as `cogs/cerberus.py`'s `_start_lockdown`.
- **Note:** the lockdown's internal `channel_overwrites`/`protected_role_overwrites` restore-snapshot maps are never exposed here — they're an internal restoration mechanism, not status information.

### GET `/api/guilds/{gid}/palantir`

**Tier:** spicy

Palantir (surveillance/audit logging) **configuration** plus a cached-message **count**. This is the API's surveillance boundary: palantir's own `palantir_messages.json` caches message content, author ids, attachment URLs, and edit pre-images for its delete/edit-log embeds, and a `palantir_attachments/` directory holds archived attachment bytes — **none of that is ever exposed through this API**. This endpoint reads `palantir_messages.json` solely to take `len()` of the guild's cached-message dict; it never reads or returns a cached entry's content, author, or attachment URL, and never touches `palantir_attachments/` at all.

```json
{
  "log_channel": {"id": "…", "name": "audit-log"},
  "archive_attachments": false,
  "muted_categories": ["voice"],
  "cached_messages": 4213
}
```

- Sourced from `palantir.json` (config) and `palantir_messages.json` (**count only**, via `len()`).
- `log_channel` — the configured surveillance-log channel, or `null` if unset.
- `archive_attachments` — whether attachment archiving to `palantir_attachments/` is on for this guild.
- `muted_categories` — the guild's `disabled_categories` list (e.g. `"messages"`, `"voice"`, `"roles"`, `"modactions"`, `"invites"`, `"server"`, `"members"`) — categories currently *not* being logged.
- `cached_messages` — integer count of messages currently held in palantir's on-disk content cache for this guild (used internally for edit/delete diffing); **never** a preview, sample, or list of the cached entries themselves.

### GET `/api/guilds/{gid}/verification`

**Tier:** spicy

Verification role-grant configuration.

```json
{
  "granter_role": {"id": "…", "name": "Verifier"},
  "target_role": {"id": "…", "name": "Member"},
  "welcome_channel": {"id": "…", "name": "welcome"},
  "welcome_enabled": true
}
```

- Sourced from `verification.json` (not `stats.db`).
- `granter_role` — the role required to run `.verify`, or `null` if unset.
- `target_role` — the role `.verify` grants, or `null` if unset.
- `welcome_channel` — the channel a welcome greeting is posted to on a successful `.verify`, or `null` if unset.
- `welcome_enabled` — mirrors `cogs/verification.py`'s own semantics: `welcome_channel_id is not None`. `.verification welcome disable` clears `welcome_channel_id` to `null`, which is exactly how the cog itself represents "welcome messages off" (there is no separate enabled/disabled flag).

### GET `/api/guilds/{gid}/moderation`

**Tier:** spicy

Moderation configuration: the configured mod-log channel plus the guild's currently-locked channels.

```json
{
  "mod_log_channel": {"id": "…", "name": "mod-log"},
  "locked_channels": [{"id": "…", "name": "general"}]
}
```

- `mod_log_channel` — sourced from `mod_log.json` (not `stats.db`); the channel `.modlog set` points at, or `null` if unset.
- `locked_channels` — every channel currently held by `.lock` (or a watchdog lockdown, which reuses the same mechanism), sorted by name. No `limit` parameter.
- **Note:** `channel_locks.json`'s values are the pre-lock permission-overwrite snapshots `.unlock`/lockdown-lift restore verbatim — an internal restoration mechanism, not status information, and never exposed here. Only the file's keys (locked channel ids) are read.
- **Limitation:** `channel_locks.json` is keyed by channel id only, with no guild id alongside it. A locked channel the bot can no longer resolve (deleted, or the guild's channel cache is cold) can't be attributed to any guild, so it's silently omitted from every guild's `locked_channels` — including the guild it actually belongs to.

### GET `/api/guilds/{gid}/features`

**Tier:** spicy

Per-guild `.feature` toggle state, one entry per toggleable cog.

```json
{
  "cogs": [
    {"name": "aidetect", "enabled": true, "globally_disabled": false},
    {"name": "leveling", "enabled": false, "globally_disabled": false},
    …
  ]
}
```

- `cogs` lists every toggleable cog, sorted by name. The list mirrors the bot's own `.feature list` exactly: every `cogs/*.py` except `__init__`/`storage` (not real cogs) and `management`/`help` (infra, can't disable themselves into inaccessibility) — including cogs whose behavior ignores the toggle entirely (e.g. `webapi` itself, which has no `cog_enabled` gate; toggling it is a no-op).
- `enabled` — this guild's `.feature enable`/`disable` state, sourced from `management.json`'s `guilds.<gid>.disabled_cogs`. Fails open (`true`) if `management.json` is missing or has no entry for this guild, matching `Management.is_cog_enabled`'s own fail-open semantics.
- `globally_disabled` — whether the cog's extension (`cogs.<name>`) is in `management.json`'s `global.disabled_extensions`, meaning the bot owner has unloaded it entirely — it isn't running at all, for any guild, regardless of `enabled`. A cog is effectively available iff `enabled` **and not** `globally_disabled`.
- Sourced from `management.json` (not `stats.db`). No `limit` parameter.

### GET `/api/users/{uid}/reminders`

**Tier:** self

The API's **first endpoint outside `/api/guilds/{gid}/...`** — reminders are a user-scoped, not guild-scoped, resource (see `cogs/reminders.py`), so this path takes a bare `{uid}` with no guild in it at all. A consumer must derive `{uid}` from its own authenticated session, never from user input — see the `self` tier definition above.

A user's own pending (not-yet-fired) reminders.

```json
{
  "reminders": [
    {
      "id": 3,
      "fire_at": "2026-07-18T12:00:00+00:00",
      "text": "walk the dog",
      "channel": {"id": "123456789012345678", "name": "general"},
      "guild": {"id": "…", "name": "…", "icon": null}
    }
  ]
}
```

- Sourced from `reminders.json` (not `stats.db`), filtered to `user_id == {uid}`.
- `reminders` — sorted by `fire_at` ascending (soonest first), matching the order the bot's own `.reminders` command lists them in.
- No `limit` parameter: the bot caps pending reminders at 10 per user (`MAX_REMINDERS_PER_USER` in `cogs/reminders.py`), so the full list is already short.
- `fire_at` is ISO8601 UTC with an explicit offset (`datetime.fromtimestamp(..., tz=timezone.utc).isoformat()`), the same format `/warnings`' `timestamp` uses — not the raw Unix float `reminders.json` stores on disk.
- `id` is a **plain JSON integer**, a documented exception to the string-snowflake rule above: it's a small per-reminder counter (`reminders.json`'s `next_id`), not a Discord snowflake, so it never approaches the 2^53 precision boundary and doesn't need string encoding.
- `channel` is resolved via the bot's **global** `self.bot.get_channel` (this endpoint has no guild to scope a lookup to, unlike the Channel object described above, which uses `guild.get_channel`):
  - a resolvable guild channel — `{"id": "…", "name": channel.name}`, with `guild` set to that channel's Guild object.
  - a resolvable but guild-less channel (a cached DM channel) — `{"id": "…", "name": "DM"}`, with `guild: null`.
  - unresolvable (deleted, or not in cache) — `{"id": "…", "name": "unknown-channel"}`, with `guild: null`.
  - `channel.id` is always a string, per the snowflake convention, regardless of which case applies.
- **Errors:** `400 {"error": "invalid user id"}` for a non-integer `{uid}`, mirroring `/members/{uid}`'s parsing. An unknown `{uid}` (the bot has never seen this user) or a user with no pending reminders is **not** an error — both return `{"reminders": []}`, the same "absence isn't an error" semantics `/members/{uid}` uses for stats. The guild-specific error rows (`400 invalid guild id`, `404 unknown guild`) don't apply — this endpoint has no `{gid}`.

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
