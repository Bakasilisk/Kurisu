import asyncio
import io
import json
import logging
import math
import os
import random
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from .management import cog_enabled, has_permissions_or_owner, rank_of, reply_ephemeral_aware
from .storage import data_path

logger = logging.getLogger(__name__)

# First non-JSON cog: raw time-series + aggregation strains the "whole file in
# memory" JSON pattern the rest of the repo uses, so this one owns a small
# SQLite database instead (stdlib sqlite3, no new dependency). All DB logic
# stays inside this module — a helper file in cogs/ would be picked up by
# management._discover_cogs() as a phantom toggleable cog.
STATS_DB = os.environ.get("STATS_DB_PATH") or data_path("stats.db")
FONT_PATH = data_path(os.path.join("assets", "fonts", "DejaVuSans.ttf"))

# Config: module constants, no live config command — matches palantir's
# deferred-config convention (tune later if real usage justifies it).
RETENTION_DAYS = 0  # 0 = keep everything; >0 enables the nightly sweep
FLUSH_INTERVAL_SECONDS = 30
TREND_PERIOD_DAYS = 30
TOP_N = 5
TOP_N_MAX = 25
BACKFILL_DEFAULT_DAYS = 0  # 0 = entire server history (all readable channels)
BACKFILL_CHANNEL_SLEEP = 1.0  # seconds between channels during backfill

BAR_WIDTH = 20
SPARK_GLYPHS = "▁▂▃▄▅▆▇█"

# Single source of truth for the period vocabulary: window lengths live here,
# PERIODS (and the converter's choices/error text) are derived from it.
_PERIOD_DAYS = {"week": 7, "month": 30, "year": 365}
PERIODS = (*_PERIOD_DAYS, "all")


class PeriodConverter(commands.Converter, app_commands.Transformer):
    """One self-enforcing period argument for both command paths. Not a
    Literal: discord.py's Literal match is `value == literal`
    (converter.py:1453), so `.stats top Week` would raise BadLiteralArgument.
    Annotating a param with this class gives the prefix path case-insensitive
    conversion (BadArgument on a miss, surfaced by cog_command_error) and the
    slash path the same values as a dropdown via `choices` — a new subcommand
    only annotates the parameter, there is no separate normalization call to
    forget (hybrid.py passes Transformer annotations through as-is)."""

    @property
    def choices(self) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=p, value=p) for p in PERIODS]

    async def convert(self, ctx, argument: str) -> str:
        return self._validate(argument)

    async def transform(self, interaction, value: str) -> str:
        return self._validate(value)

    @staticmethod
    def _validate(raw: str) -> str:
        normalized = raw.strip().lower()
        if normalized not in PERIODS:
            # Static message, no raw-input interpolation: cog_command_error
            # echoes BadArgument text publicly, so reflecting the argument
            # would let any member bounce mentions/markdown off the bot
            # (same rule as moderation.parse_duration).
            raise commands.BadArgument(f"Period must be one of {', '.join(PERIODS)}.")
        return normalized


# --- Presentation helpers (module-level, no self needed) --------------------


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    vmax = max(values) or 1
    return "".join(
        SPARK_GLYPHS[min(len(SPARK_GLYPHS) - 1, int((v / vmax) * (len(SPARK_GLYPHS) - 1)))]
        for v in values
    )


def _format_distribution(entries: list[tuple[str, int]], limit: int, unit=str) -> str:
    """Bar-chart lines for the top `limit` entries (label, count) sorted desc,
    plus a "…and N others X%" remainder line so percentages always sum to
    ~100% even though only the top slice is rendered. `entries` must contain
    every item (not just the ones being displayed) for the total to be right."""
    total = sum(count for _, count in entries)
    if not entries or total == 0:
        return "No data yet."
    lines = []
    shown = 0
    for label, count in entries[:limit]:
        pct = count / total * 100
        shown += count
        lines.append(f"{_bar(count / total)} {label} — {unit(count)} ({pct:.1f}%)")
    remainder_n = len(entries) - min(limit, len(entries))
    remainder_count = total - shown
    if remainder_n > 0:
        pct = remainder_count / total * 100
        lines.append(f"…and {remainder_n} others — {unit(remainder_count)} ({pct:.1f}%)")
    return "\n".join(lines)


def _render_heatmap(grid: list[list[int]], weekday_labels: list[str]) -> io.BytesIO:
    """Render a 7 (weekday) x 24 (hour) heatmap PNG, cells color-scaled
    light-to-dark by count, axis labels drawn with the bundled DejaVu Sans
    font (never PIL's default font — see captions.py's FONT_PATH note).
    Raises on any failure; the caller falls back to a unicode-bar summary."""
    cell = 22
    margin_left = 34
    margin_top = 26
    margin_bottom = 8
    margin_right = 8
    width = margin_left + 24 * cell + margin_right
    height = margin_top + 7 * cell + margin_bottom

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT_PATH, size=11)

    vmax = max((v for row in grid for v in row), default=0) or 1
    low = (233, 238, 252)  # near-white blue: zero activity
    high = (25, 55, 150)  # deep blue: peak activity

    for weekday in range(7):
        for hour in range(24):
            count = grid[weekday][hour]
            t = count / vmax
            color = tuple(int(low[i] + (high[i] - low[i]) * t) for i in range(3))
            x0 = margin_left + hour * cell
            y0 = margin_top + weekday * cell
            draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=color, outline="#cccccc")

    for weekday, label in enumerate(weekday_labels):
        y = margin_top + weekday * cell + cell / 2
        draw.text((3, y), label, font=font, fill="black", anchor="lm")

    for hour in range(0, 24, 3):
        x = margin_left + hour * cell + cell / 2
        draw.text((x, margin_top - 4), str(hour), font=font, fill="black", anchor="mb")

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


# --- Kurisu Wrapped (yearly recap) ------------------------------------------
#
# A German five-page story over one calendar year plus a PNG card on the last
# page. Kurisu's comments live in assets/wrapped/lines.json (approved text —
# edit wording there, never here); this block only picks a variant per
# bucket and fills its {placeholders}. Everything here is pure (no bot, no
# DB) so it can be exercised standalone. Stored days/hours stay UTC; only the
# busiest hour/weekday are converted to Berlin local time, for display.

WRAPPED_LINES_PATH = data_path(os.path.join("assets", "wrapped", "lines.json"))
WRAPPED_MIN_YEAR = 2015  # the year Discord launched
WRAPPED_VIEW_TIMEOUT = 300
WRAPPED_CARD_FILENAME = "wrapped.png"
WRAPPED_COLOR = discord.Color.from_rgb(165, 32, 58)
WRAPPED_STREAK_HIGH = 14  # days in a row for the "high" streak comment

try:
    WRAPPED_TZ = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    # No system tz database and no tzdata package: show UTC rather than fail
    # the import (and with it the whole stats cog).
    logger.warning("Stats: Europe/Berlin timezone unavailable, Wrapped falls back to UTC")
    WRAPPED_TZ = timezone.utc

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTH_INITIALS = "JFMAMJJASOND"

# Every list of variants the commands pick from, as key paths into
# lines.json — checked once at load so a broken edit disables Wrapped
# cleanly instead of raising KeyError halfway through a command.
_WRAPPED_LINE_PATHS = (
    *(("user", "messages", b) for b in ("high", "mid", "low")),
    *(("user", "channels", b) for b in ("default", "single")),
    *(("user", "time", b) for b in ("night", "morning", "day", "evening")),
    *(("user", "streak", b) for b in ("high", "low")),
    *(("user", "voice", b) for b in ("none", "low", "mid", "high")),
    ("user", "reactions"),
    *(("user", "finale", b) for b in ("top1", "top10", "other")),
    ("user", "empty"),
    ("server", "intro"),
    ("server", "top"),
    ("server", "time"),
    *(("server", "growth", b) for b in ("positive", "negative")),
    ("server", "finale"),
    ("server", "empty"),
)

# The placeholder keys each section is filled with — must match what
# _user_placeholders / _server_placeholders return. Every variant is
# format-checked against these at load, so a typo like {mesages} disables
# Wrapped up front instead of failing only on the runs that pick it.
_WRAPPED_USER_KEYS = (
    "year", "messages", "rank", "percent", "channel", "share", "month", "hour", "weekday",
    "days", "streak", "voice", "voice_days", "given", "received",
)
_WRAPPED_SERVER_KEYS = ("year", "messages", "members", "top_user", "month", "day", "joins", "leaves", "net")
_WRAPPED_DUMMY = {
    "user": dict.fromkeys(_WRAPPED_USER_KEYS, "x"),
    "server": {**dict.fromkeys(_WRAPPED_SERVER_KEYS, "x"), "net": 1},  # {net:+} needs an int
}


def _load_wrapped_lines(path: str = WRAPPED_LINES_PATH) -> dict | None:
    """Load and shape-check lines.json, or None (logged) if it's missing,
    unreadable or lacks a variant list — the commands then reply that Wrapped
    is unavailable instead of crashing the cog. Explicit UTF-8: the text is
    full of umlauts and the service locale isn't guaranteed to be UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = json.load(f)
        for key_path in _WRAPPED_LINE_PATHS:
            node = lines
            for key in key_path:
                node = node[key]
            if not isinstance(node, list) or not node or not all(isinstance(v, str) for v in node):
                raise ValueError(f"{'.'.join(key_path)} must be a non-empty list of strings")
            for variant in node:
                variant.format(**_WRAPPED_DUMMY[key_path[0]])
        if not isinstance(lines["share"], str):
            raise ValueError("share must be a string")
        lines["share"].format(year=2026, user="x")
    except Exception:
        logger.exception("Stats: couldn't load Wrapped lines from %s, Wrapped is disabled", path)
        return None
    return lines


def _de_num(n) -> str:
    """German thousands separator: 12403 -> '12.403'."""
    return f"{int(n):,}".replace(",", ".")


def _de_percent(p) -> str:
    return f"{p} %"


def _de_hour(hour: int) -> str:
    return f"{hour:02d}:00 Uhr"


def _de_date(day: str) -> str:
    """'2026-03-29' -> '29.03.2026'."""
    return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")


def _de_days(n: int) -> str:
    return f"{_de_num(n)} Tag" if n == 1 else f"{_de_num(n)} Tage"


def _de_voice(seconds) -> str:
    """Voice time as whole hours from one hour up ('214 Stunden'), minutes
    below that ('37 Minuten'); any non-zero time is at least '1 Minute'."""
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600:
        hours = seconds // 3600
        return f"{_de_num(hours)} Stunde" if hours == 1 else f"{_de_num(hours)} Stunden"
    minutes = max(1, seconds // 60) if seconds else 0
    return f"{minutes} Minute" if minutes == 1 else f"{minutes} Minuten"


def _de_voice_days(seconds) -> str:
    """Voice time in days, one decimal, German comma: 770400 s -> '8,9'."""
    return f"{max(0, int(seconds or 0)) / 86400:.1f}".replace(".", ",")


def _longest_streak(days) -> int:
    """Longest run of consecutive calendar days among `days` ('YYYY-MM-DD'
    strings, any order, duplicates fine); malformed entries are skipped."""
    parsed = set()
    for day in days:
        try:
            parsed.add(date.fromisoformat(day))
        except (TypeError, ValueError):
            continue
    best = run = 0
    previous = None
    for current in sorted(parsed):
        run = run + 1 if previous is not None and (current - previous).days == 1 else 1
        best = max(best, run)
        previous = current
    return best


def _local_activity(rows, tz=WRAPPED_TZ) -> tuple[list[int], list[int]]:
    """Sum UTC `hourly` rows (day, hour, count) into local hour-of-day (24)
    and weekday (7, Monday first) totals. Converted per row rather than by a
    fixed offset, so each row lands on the right side of a DST switch (and
    a late-evening UTC row can roll over to the next local weekday)."""
    hours = [0] * 24
    weekdays = [0] * 7
    for day, hour, count in rows:
        try:
            utc = datetime.strptime(day, "%Y-%m-%d").replace(hour=int(hour), tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        local = utc.astimezone(tz)
        hours[local.hour] += count
        weekdays[local.weekday()] += count
    return hours, weekdays


def _peak(values) -> int | None:
    """Index of the largest value (earliest wins a tie), None if all are zero."""
    if not values or max(values) <= 0:
        return None
    return max(range(len(values)), key=lambda i: values[i])


def _top_percent(rank: int | None, active_users: int) -> int | None:
    """'Top X %' — the share of active users at or above this rank, rounded up."""
    if not rank or not active_users:
        return None
    return math.ceil(rank / active_users * 100)


def _messages_bucket(percent: int | None) -> str:
    if percent is not None and percent <= 10:
        return "high"
    if percent is not None and percent <= 50:
        return "mid"
    return "low"


def _channels_bucket(channel_count: int, top_share: float) -> str:
    return "single" if channel_count == 1 or top_share >= 0.9 else "default"


def _time_bucket(hour: int) -> str:
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "day"
    return "evening"


def _streak_bucket(streak: int) -> str:
    return "high" if streak >= WRAPPED_STREAK_HIGH else "low"


def _voice_bucket(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "none"
    if seconds < 10 * 3600:
        return "low"
    if seconds < 100 * 3600:
        return "mid"
    return "high"


def _finale_bucket(rank: int | None) -> str:
    if rank == 1:
        return "top1"
    if rank is not None and rank <= 10:
        return "top10"
    return "other"


def _growth_bucket(net: int) -> str:
    return "positive" if net >= 0 else "negative"


def _pick_line(variants: list[str], placeholders: dict, rng=random) -> str:
    """One random variant, filled. Extra placeholder keys are ignored by
    str.format, so each section can be handed the full placeholder dict."""
    return rng.choice(variants).format(**placeholders)


def _wrapped_title(year: int, current_year: int) -> str:
    return f"Kurisu Wrapped {year} (bis jetzt)" if year == current_year else f"Kurisu Wrapped {year}"


def _user_placeholders(data: dict) -> dict:
    """Every placeholder the user-story lines may use, display-formatted.
    {channel} is a mention (lines only ever render inside an embed)."""
    top = data["channels"][0] if data["channels"] else None
    month, hour, weekday = data["busiest_month"], data["busiest_hour"], data["busiest_weekday"]
    return {
        "year": data["year"],
        "messages": _de_num(data["messages"]),
        "rank": data["rank"] if data["rank"] is not None else "–",
        "percent": _de_percent(data["percent"]) if data["percent"] is not None else "–",
        "channel": top["mention"] if top else "–",
        "share": _de_percent(round(top["share"] * 100)) if top else "–",
        "month": MONTHS_DE[month] if month is not None else "–",
        "hour": _de_hour(hour) if hour is not None else "–",
        "weekday": WEEKDAYS_DE[weekday] if weekday is not None else "–",
        "days": _de_num(data["active_days"]),
        "streak": _de_num(data["streak"]),
        "voice": _de_voice(data["voice_seconds"]),
        "voice_days": _de_voice_days(data["voice_seconds"]),
        "given": _de_num(data["given"]),
        "received": _de_num(data["received"]),
    }


def _server_placeholders(data: dict) -> dict:
    """Every placeholder the server-story lines may use. {net} stays an int —
    the lines apply their own {net:+} format spec."""
    top = data["top_users"][0] if data["top_users"] else None
    month = data["busiest_month"]
    return {
        "year": data["year"],
        "messages": _de_num(data["messages"]),
        "members": _de_num(data["members"]),
        "top_user": top["mention"] if top else "–",
        "month": MONTHS_DE[month] if month is not None else "–",
        "day": _de_date(data["busiest_day"]) if data["busiest_day"] else "–",
        "joins": _de_num(data["joins"]),
        "leaves": _de_num(data["leaves"]),
        "net": data["net"],
    }


def _monthly_field(monthly: list[int]) -> str:
    # Both lines in one code block so the month initials sit under their bar.
    return f"```\n{_sparkline(monthly)}\n{MONTH_INITIALS}\n```"


def _wrapped_embed(data: dict, page: int, heading: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{data['title']} — {heading}", description=description or None, color=WRAPPED_COLOR
    )
    if data.get("icon_url"):
        embed.set_author(name=data["name"], icon_url=data["icon_url"])
    else:
        embed.set_author(name=data["name"])
    embed.set_footer(text=f"Seite {page}/5")
    return embed


def _build_user_pages(data: dict, lines: dict, has_card: bool, rng=random) -> list[discord.Embed]:
    """The five embeds of a member's story. Numbers go in fields, Kurisu's
    comment in the description; page 5 shows the card when it rendered and
    the same highlights as text otherwise."""
    u = lines["user"]
    p = _user_placeholders(data)
    channels = data["channels"]
    rank = data["rank"]
    rank_text = f"#{rank} von {_de_num(data['active_users'])}" if rank else "–"
    top_text = f"Top {_de_percent(data['percent'])}" if data["percent"] is not None else "–"
    month = data["busiest_month"]
    month_text = f"{MONTHS_DE[month]} ({_de_num(data['monthly'][month])})" if month is not None else "–"
    hour_text = _de_hour(data["busiest_hour"]) if data["busiest_hour"] is not None else "–"
    voice_text = _de_voice(data["voice_seconds"]) if data["voice_seconds"] > 0 else "–"

    page1 = _wrapped_embed(data, 1, "Nachrichten", _pick_line(u["messages"][_messages_bucket(data["percent"])], p, rng))
    page1.add_field(name="Nachrichten", value=_de_num(data["messages"]), inline=True)
    page1.add_field(name="Rang", value=rank_text, inline=True)
    page1.add_field(name="Anteil", value=top_text, inline=True)
    page1.add_field(name="Wörter/Nachricht", value=f"{data['words_per_msg']:.1f}".replace(".", ","), inline=True)

    if channels:
        bucket = _channels_bucket(len(channels), channels[0]["share"])
        page2 = _wrapped_embed(data, 2, "Kanäle", _pick_line(u["channels"][bucket], p, rng))
        top3 = "\n".join(
            f"{i}. {c['mention']} — {_de_num(c['count'])} ({_de_percent(round(c['share'] * 100))})"
            for i, c in enumerate(channels[:3], start=1)
        )
    else:
        page2 = _wrapped_embed(data, 2, "Kanäle", "")
        top3 = "–"
    page2.add_field(name="Top-Kanäle", value=top3, inline=False)
    page2.add_field(name="Stärkster Monat", value=month_text, inline=True)
    page2.add_field(name="Verlauf", value=_monthly_field(data["monthly"]), inline=False)

    # No hourly data means no messages at all (both tables are written
    # together), so there's no hour to comment on — only the streak line.
    comments = []
    if data["busiest_hour"] is not None:
        comments.append(_pick_line(u["time"][_time_bucket(data["busiest_hour"])], p, rng))
    comments.append(_pick_line(u["streak"][_streak_bucket(data["streak"])], p, rng))
    page3 = _wrapped_embed(data, 3, "Aktivität", "\n\n".join(comments))
    page3.add_field(name="Aktivste Uhrzeit", value=hour_text, inline=True)
    page3.add_field(
        name="Lieblingstag",
        value=WEEKDAYS_DE[data["busiest_weekday"]] if data["busiest_weekday"] is not None else "–",
        inline=True,
    )
    page3.add_field(name="Aktive Tage", value=_de_num(data["active_days"]), inline=True)
    page3.add_field(name="Längste Serie", value=_de_days(data["streak"]), inline=True)

    page4 = _wrapped_embed(
        data, 4, "Voice & Reaktionen",
        _pick_line(u["voice"][_voice_bucket(data["voice_seconds"])], p, rng)
        + "\n\n" + _pick_line(u["reactions"], p, rng),
    )
    page4.add_field(name="Voice-Zeit", value=voice_text, inline=True)
    page4.add_field(name="Reaktionen gegeben", value=_de_num(data["given"]), inline=True)
    page4.add_field(name="Reaktionen bekommen", value=_de_num(data["received"]), inline=True)

    page5 = _wrapped_embed(data, 5, "Karte", _pick_line(u["finale"][_finale_bucket(rank)], p, rng))
    if has_card:
        page5.set_image(url=f"attachment://{WRAPPED_CARD_FILENAME}")
    else:
        page5.add_field(name="Nachrichten", value=_de_num(data["messages"]), inline=True)
        page5.add_field(name="Rang", value=rank_text, inline=True)
        page5.add_field(name="Anteil", value=top_text, inline=True)
        page5.add_field(name="Lieblingskanal", value=channels[0]["mention"] if channels else "–", inline=True)
        page5.add_field(name="Aktivste Uhrzeit", value=hour_text, inline=True)
        page5.add_field(name="Voice-Zeit", value=voice_text, inline=True)
        page5.add_field(name="Verlauf", value=_monthly_field(data["monthly"]), inline=False)
    return [page1, page2, page3, page4, page5]


def _build_server_pages(data: dict, lines: dict, has_card: bool, rng=random) -> list[discord.Embed]:
    """The five embeds of the server story — same layout rules as the user's."""
    s = lines["server"]
    p = _server_placeholders(data)
    total = data["messages"] or 1
    month = data["busiest_month"]
    month_text = f"{MONTHS_DE[month]} ({_de_num(data['monthly'][month])})" if month is not None else "–"
    day_text = (
        f"{_de_date(data['busiest_day'])} ({_de_num(data['busiest_day_count'])})" if data["busiest_day"] else "–"
    )

    def ranking(entries):
        return "\n".join(
            f"{i}. {e['mention']} — {_de_num(e['count'])} ({_de_percent(round(e['count'] / total * 100))})"
            for i, e in enumerate(entries, start=1)
        ) or "–"

    page1 = _wrapped_embed(data, 1, "Überblick", _pick_line(s["intro"], p, rng))
    page1.add_field(name="Nachrichten", value=_de_num(data["messages"]), inline=True)
    page1.add_field(name="Aktive Mitglieder", value=_de_num(data["members"]), inline=True)

    page2 = _wrapped_embed(data, 2, "Top-Poster & Kanäle", _pick_line(s["top"], p, rng))
    page2.add_field(name="Top-Poster", value=ranking(data["top_users"]), inline=False)
    page2.add_field(name="Top-Kanäle", value=ranking(data["top_channels"]), inline=False)

    page3 = _wrapped_embed(data, 3, "Zeit", _pick_line(s["time"], p, rng))
    page3.add_field(name="Stärkster Monat", value=month_text, inline=True)
    page3.add_field(name="Aktivster Tag", value=day_text, inline=True)
    page3.add_field(name="Verlauf", value=_monthly_field(data["monthly"]), inline=False)

    page4 = _wrapped_embed(data, 4, "Wachstum", _pick_line(s["growth"][_growth_bucket(data["net"])], p, rng))
    page4.add_field(name="Beitritte", value=_de_num(data["joins"]), inline=True)
    page4.add_field(name="Austritte", value=_de_num(data["leaves"]), inline=True)
    page4.add_field(name="Netto", value=f"{data['net']:+,}".replace(",", "."), inline=True)

    page5 = _wrapped_embed(data, 5, "Karte", _pick_line(s["finale"], p, rng))
    if has_card:
        page5.set_image(url=f"attachment://{WRAPPED_CARD_FILENAME}")
    else:
        page5.add_field(name="Nachrichten", value=_de_num(data["messages"]), inline=True)
        page5.add_field(name="Aktive Mitglieder", value=_de_num(data["members"]), inline=True)
        page5.add_field(name="Stärkster Monat", value=month_text, inline=True)
        page5.add_field(name="Top-Poster", value=ranking(data["top_users"][:3]), inline=False)
        page5.add_field(
            name="Top-Kanal", value=data["top_channels"][0]["mention"] if data["top_channels"] else "–", inline=True
        )
        page5.add_field(name="Verlauf", value=_monthly_field(data["monthly"]), inline=False)
    return [page1, page2, page3, page4, page5]


def _card_text(text: str, fallback: str = "Unbekannt") -> str:
    """Drop what DejaVu Sans can't draw anyway (astral-plane emoji, variation
    selectors, zero-width joiners) so a fancy display name doesn't turn into
    a row of tofu boxes on the card."""
    cleaned = "".join(
        ch for ch in (text or "") if ord(ch) <= 0xFFFF and not 0xFE00 <= ord(ch) <= 0xFE0F and ch != "‍"
    )
    return " ".join(cleaned.split()) or fallback


def _render_wrapped_card(data: dict) -> io.BytesIO:
    """Render the 800x1000 Wrapped card PNG (user or server story, by
    data['kind']) with the bundled DejaVu Sans font: gradient background,
    title, round avatar/guild icon (a lettered placeholder if there's none
    or it won't decode), headline message count, a stat grid and 12 monthly
    bars. Text only — no emoji, DejaVu can't render colour glyphs. Raises
    on any other failure; the caller falls back to a text-only last page."""
    width, height = 800, 1000
    bg_top, bg_bottom = (17, 21, 48), (80, 18, 40)  # deep navy -> maroon
    accent = (255, 94, 110)
    text = (242, 242, 247)
    muted = (172, 176, 204)
    bar_idle = (214, 170, 190)

    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / (height - 1)
        draw.line([(0, y), (width, y)], fill=tuple(int(bg_top[i] + (bg_bottom[i] - bg_top[i]) * t) for i in range(3)))

    fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def font(size: int) -> ImageFont.FreeTypeFont:
        if size not in fonts:
            fonts[size] = ImageFont.truetype(FONT_PATH, size=size)
        return fonts[size]

    def fit(value: str, size: int, max_width: float) -> str:
        if font(size).getlength(value) <= max_width:
            return value
        while value and font(size).getlength(value + "…") > max_width:
            value = value[:-1]
        return value + "…"

    # Title (DejaVu Sans has no bold face here; a 1px stroke fakes it).
    draw.text((width / 2, 52), f"KURISU WRAPPED {data['year']}", font=font(42), fill=accent, anchor="mm",
              stroke_width=1, stroke_fill=accent)
    if data["partial"]:
        draw.text((width / 2, 90), "BIS JETZT", font=font(16), fill=muted, anchor="mm")

    # Avatar / guild icon, circular, with an accent ring.
    size = 160
    left, top = int(width / 2 - size / 2), 110
    name = _card_text(data["card_name"])
    avatar = None
    if data.get("avatar"):
        try:
            avatar = Image.open(io.BytesIO(data["avatar"])).convert("RGB").resize((size, size), Image.LANCZOS)
        except (OSError, ValueError):
            avatar = None  # undecodable bytes: fall through to the placeholder
    if avatar is not None:
        # Draw the mask at 4x and downsample for a smooth (anti-aliased) edge.
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
        image.paste(avatar, (left, top), mask.resize((size, size), Image.LANCZOS))
    else:
        draw.ellipse([left, top, left + size - 1, top + size - 1], fill=(58, 40, 86))
        draw.text((width / 2, top + size / 2), name[0].upper(), font=font(72), fill=text, anchor="mm")
    draw.ellipse([left - 4, top - 4, left + size + 3, top + size + 3], outline=accent, width=4)

    draw.text((width / 2, 312), fit(name, 34, width - 80), font=font(34), fill=text, anchor="mm")

    # Headline number.
    draw.text((width / 2, 395), _de_num(data["messages"]), font=font(96), fill=text, anchor="mm",
              stroke_width=2, stroke_fill=text)
    draw.text((width / 2, 460), "NACHRICHTEN", font=font(18), fill=muted, anchor="mm")

    # Stat grid: two columns of label/value pairs.
    def cell(column: int, y: int, label: str, value: str) -> None:
        x = 80 if column == 0 else width / 2 + 20
        draw.text((x, y), label, font=font(15), fill=muted, anchor="ls")
        draw.text((x, y + 36), fit(value, 28, width / 2 - 100), font=font(28), fill=text, anchor="ls")

    month = data["busiest_month"]
    month_name = MONTHS_DE[month] if month is not None else "–"
    if data["kind"] == "user":
        channel = data["channels"][0]["name"] if data["channels"] else "–"
        rank_value = f"#{data['rank']} von {_de_num(data['active_users'])}" if data["rank"] else "–"
        cell(0, 520, "RANG", rank_value)
        cell(1, 520, "ANTEIL", f"Top {_de_percent(data['percent'])}" if data["percent"] is not None else "–")
        cell(0, 600, "LIEBLINGSKANAL", _card_text(channel, "–"))
        cell(1, 600, "AKTIVSTE UHRZEIT", _de_hour(data["busiest_hour"]) if data["busiest_hour"] is not None else "–")
        cell(0, 680, "VOICE", _de_voice(data["voice_seconds"]) if data["voice_seconds"] > 0 else "–")
        cell(1, 680, "LÄNGSTE SERIE", _de_days(data["streak"]))
    else:
        channel = data["top_channels"][0]["name"] if data["top_channels"] else "–"
        cell(0, 520, "AKTIVE MITGLIEDER", _de_num(data["members"]))
        cell(1, 520, "STÄRKSTER MONAT", month_name)
        cell(0, 600, "TOP-KANAL", _card_text(channel, "–"))
        cell(1, 600, "NETTO-WACHSTUM", f"{data['net']:+,}".replace(",", "."))
        draw.text((80, 680), "TOP-POSTER", font=font(15), fill=muted, anchor="ls")
        for i, entry in enumerate(data["top_users"][:3]):
            line = f"{i + 1}. {_card_text(entry['name'])}"
            draw.text((80, 712 + i * 30), fit(line, 22, width / 2 + 60), font=font(22), fill=text, anchor="ls")
            draw.text((width - 80, 712 + i * 30), _de_num(entry["count"]), font=font(22), fill=muted, anchor="rs")

    # Monthly bars, busiest month highlighted; empty months keep a thin baseline.
    monthly = data["monthly"]
    vmax = max(monthly) or 1
    area_left, area_right, base_y, max_bar = 80, width - 80, 935, 120
    slot = (area_right - area_left) / 12
    for i, count in enumerate(monthly):
        x0 = area_left + i * slot + slot * 0.2
        x1 = area_left + (i + 1) * slot - slot * 0.2
        bar = max(3, round(count / vmax * max_bar))
        draw.rectangle([x0, base_y - bar, x1, base_y], fill=accent if i == month else bar_idle)
        draw.text(((x0 + x1) / 2, base_y + 22), MONTH_INITIALS[i], font=font(15), fill=muted, anchor="mm")

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


class WrappedView(discord.ui.View):
    """◀/▶ paging for a Wrapped story, plus a one-time "share in channel"
    button on the last page. The repo's first discord.ui view, kept here so
    it never becomes its own (phantom-cog) module. Only the invoker can
    press anything. The card stays as raw PNG bytes and every attach builds
    a fresh discord.File — a File's buffer is consumed once it's sent."""

    def __init__(self, pages: list[discord.Embed], invoker_id: int, card_png: bytes | None,
                 share_content: str | None):
        super().__init__(timeout=WRAPPED_VIEW_TIMEOUT)
        self.pages = pages
        self.invoker_id = invoker_id
        self.card_png = card_png
        # None = no share button: prefix invocations are public already, and
        # without a card there's nothing worth sharing.
        self.share_content = share_content if card_png is not None else None
        self.index = 0
        self.shared = False
        self.message = None  # set by the command right after sending page 1
        self.share_button = discord.ui.Button(
            label="Im Kanal teilen", emoji="🔗", style=discord.ButtonStyle.success
        )
        self.share_button.callback = self._share
        self._sync_buttons()

    def _on_last_page(self) -> bool:
        return self.index == len(self.pages) - 1

    def card_file(self) -> discord.File:
        return discord.File(io.BytesIO(self.card_png), filename=WRAPPED_CARD_FILENAME)

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self._on_last_page()
        show_share = self.share_content is not None and self._on_last_page()
        if show_share and self.share_button not in self.children:
            self.add_item(self.share_button)
        elif not show_share and self.share_button in self.children:
            self.remove_item(self.share_button)
        self.share_button.disabled = self.shared

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message("Das ist nicht dein Wrapped.", ephemeral=True)
        return False

    async def _turn(self, interaction: discord.Interaction, step: int) -> None:
        self.index = max(0, min(len(self.pages) - 1, self.index + step))
        self._sync_buttons()
        # attachments=[] on every other page drops the card again.
        attachments = [self.card_file()] if self._on_last_page() and self.card_png is not None else []
        await interaction.response.edit_message(embed=self.pages[self.index], attachments=attachments, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, 1)

    async def _share(self, interaction: discord.Interaction) -> None:
        if self.shared:  # a double-click raced the disable below
            await interaction.response.defer()
            return
        self.shared = True
        self._sync_buttons()
        # Acknowledge by disabling the button in place, then post publicly as
        # a followup (ephemeral=False even though the story itself is private).
        await interaction.response.edit_message(view=self)
        try:
            await interaction.followup.send(
                content=self.share_content, file=self.card_file(), ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Stats: sharing a Wrapped card failed")
            self.shared = False
            self._sync_buttons()
            try:
                await interaction.edit_original_response(view=self)
                await interaction.followup.send("Teilen hat nicht geklappt. Versuch es gleich noch einmal.",
                                                ephemeral=True)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # deleted, or the slash token's 15 minutes ran out


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        self._db = sqlite3.connect(STATS_DB, check_same_thread=False)
        # Guards every access to self._db — asyncio.to_thread hands DB calls
        # to the default thread pool, and check_same_thread=False lets any of
        # those worker threads use the connection, so this lock is what
        # actually serializes them (same reasoning as the JSON cogs' locks,
        # just for a shared connection instead of an in-memory dict).
        self._db_lock = threading.Lock()
        self._init_schema()

        # In-memory accumulators, drained to SQLite by _flush_stats every
        # FLUSH_INTERVAL_SECONDS (the repo's buffer-then-flush rhythm — see
        # leveling/palantir). Keyed by the same tuples as each table's
        # primary key; values are mutable lists so increments are in place.
        self._msg_acc: dict[tuple[int, str, int, int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        self._hourly_acc: dict[tuple[int, str, int, int, int], int] = defaultdict(int)
        self._reaction_acc: dict[tuple[int, str, int], list[int]] = defaultdict(lambda: [0, 0])
        self._voice_acc: dict[tuple[int, str, int], int] = defaultdict(int)
        self._membership_acc: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
        # guild_id -> earliest ISO8601 live_since seen this buffer cycle
        # (earliest-wins merge in _merge_back; first-write-wins at the DB
        # level via INSERT OR IGNORE in _write_snapshot).
        self._meta_acc: dict[int, str] = {}
        self._acc_lock = asyncio.Lock()
        self._dirty = False

        # (guild_id, user_id) -> monotonic timestamp of the last voice-channel
        # join/move, used to compute elapsed seconds on the next leave/move.
        # Best-effort: an in-flight session is lost across a restart.
        self._voice_sessions: dict[tuple[int, int], float] = {}

        self._backfill_in_progress: set[int] = set()

        # Wrapped's comment text, loaded once; None disables only the two
        # Wrapped commands (logged by the loader), never the rest of the cog.
        self._wrapped_lines = _load_wrapped_lines()

        if not self._flush_stats.is_running():
            self._flush_stats.start()
        if RETENTION_DAYS > 0 and not self._sweep_stats.is_running():
            self._sweep_stats.start()

    def cog_unload(self):
        self._flush_stats.cancel()
        if self._sweep_stats.is_running():
            self._sweep_stats.cancel()
        # cog_unload can't be a coroutine, so it can't await self._acc_lock.
        # If a flush is already in flight on the thread pool, that write
        # already carries the latest data, so skip ours rather than race it
        # (same reasoning as leveling/palantir's cog_unload).
        if not self._acc_lock.locked() and self._dirty:
            snapshot = self._drain_accumulators_unsafe()
            try:
                self._write_snapshot(*snapshot)
            except Exception:
                logger.exception("Stats: final flush on unload failed")
        self._db.close()

    # --- Schema ---------------------------------------------------------

    def _init_schema(self) -> None:
        with self._db_lock:
            self._db.execute("PRAGMA journal_mode=WAL")

            # Migration guard: an existing pre-`src` DB has a `messages` table
            # with no `src` column — rebuild it (and `hourly`) in place, once.
            # A brand-new DB has no `messages` table yet, so `cols` is empty
            # and CREATE TABLE IF NOT EXISTS below just creates the new
            # schema directly — no migration needed.
            cols = {row[1] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()}
            if cols and "src" not in cols:
                self._migrate_add_src_unsafe()

            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    words INTEGER NOT NULL DEFAULT 0,
                    chars INTEGER NOT NULL DEFAULT 0,
                    src INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id, channel_id, src)
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_guild_day ON messages (guild_id, day)"
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS hourly (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    src INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id, hour, src)
                )
                """
            )
            # Supports the guild-wide hour-of-day / heatmap aggregation, which
            # sums over all users (no user_id filter) — the PK's leading
            # (guild_id, day) prefix covers it, but an explicit index keeps
            # that intent clear and independent of PK column order.
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_hourly_guild_day ON hourly (guild_id, day)"
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS reactions (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    given INTEGER NOT NULL DEFAULT 0,
                    received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id)
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS voice (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id)
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS membership (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day)
                )
                """
            )
            # The instant collection began for a guild, set once by whichever
            # of on_message / backfill gets there first (INSERT OR IGNORE —
            # earliest wins). _run_backfill uses this as the timestamp
            # boundary: seed everything strictly before it, skip everything
            # at/after it (owned by live ingest) — see _run_backfill.
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    guild_id INTEGER NOT NULL PRIMARY KEY,
                    live_since TEXT NOT NULL
                )
                """
            )
            self._db.execute("DROP TABLE IF EXISTS live_days")
            self._db.commit()

    def _migrate_add_src_unsafe(self) -> None:
        """Rebuild `messages`/`hourly` with `src` added to the PRIMARY KEY,
        migrating existing rows in as src=0 (they were live/legacy) — SQLite
        can't ALTER a PRIMARY KEY, so this is the standard rename-rebuild.
        Caller must already hold _db_lock; runs at most once per DB, gated by
        the `src`-column check in _init_schema."""
        self._db.execute("ALTER TABLE messages RENAME TO messages_old")
        self._db.execute(
            """
            CREATE TABLE messages (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                words INTEGER NOT NULL DEFAULT 0,
                chars INTEGER NOT NULL DEFAULT 0,
                src INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, user_id, channel_id, src)
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO messages (guild_id, day, user_id, channel_id, count, words, chars, src)
            SELECT guild_id, day, user_id, channel_id, count, words, chars, 0 FROM messages_old
            """
        )
        self._db.execute("DROP TABLE messages_old")

        self._db.execute("ALTER TABLE hourly RENAME TO hourly_old")
        self._db.execute(
            """
            CREATE TABLE hourly (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                src INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, user_id, hour, src)
            )
            """
        )
        self._db.execute(
            """
            INSERT INTO hourly (guild_id, day, user_id, hour, count, src)
            SELECT guild_id, day, user_id, hour, count, 0 FROM hourly_old
            """
        )
        self._db.execute("DROP TABLE hourly_old")
        # Indexes on the old tables are dropped along with them above; the
        # CREATE INDEX IF NOT EXISTS statements right after this call (in
        # _init_schema) recreate them on the rebuilt tables.
        self._db.commit()

    # --- Buffer -> flush --------------------------------------------------

    def _drain_accumulators_unsafe(self):
        """Snapshot and clear every accumulator dict. Caller must already
        hold _acc_lock, or (only at shutdown in cog_unload) be certain
        nothing else can be touching it concurrently."""
        snapshot = (
            dict(self._msg_acc),
            dict(self._hourly_acc),
            dict(self._reaction_acc),
            dict(self._voice_acc),
            dict(self._membership_acc),
            dict(self._meta_acc),
        )
        self._msg_acc.clear()
        self._hourly_acc.clear()
        self._reaction_acc.clear()
        self._voice_acc.clear()
        self._membership_acc.clear()
        self._meta_acc.clear()
        self._dirty = False
        return snapshot

    def _merge_back(self, snapshot) -> None:
        """Restore a drained-but-failed-to-write snapshot into the live
        accumulators (additively, so it combines correctly with anything
        accumulated in the meantime) — the retry path for a flush failure."""
        msg, hourly, reactions, voice, membership, meta = snapshot
        for key, vals in msg.items():
            cur = self._msg_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]
            cur[2] += vals[2]
        for key, count in hourly.items():
            self._hourly_acc[key] += count
        for key, vals in reactions.items():
            cur = self._reaction_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]
        for key, seconds in voice.items():
            self._voice_acc[key] += seconds
        for key, vals in membership.items():
            cur = self._membership_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]
        for g, iso in meta.items():
            cur = self._meta_acc.get(g)
            self._meta_acc[g] = iso if cur is None else min(cur, iso)

    def _write_snapshot(self, msg, hourly, reactions, voice, membership, meta) -> None:
        """Blocking: executemany UPSERT each non-empty accumulator into its
        table. Called via asyncio.to_thread from _flush_now, or directly
        (blocking is fine — we're shutting down) from cog_unload."""
        with self._db_lock:
            if msg:
                self._db.executemany(
                    """
                    INSERT INTO messages (guild_id, day, user_id, channel_id, src, count, words, chars)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id, channel_id, src) DO UPDATE SET
                        count = count + excluded.count,
                        words = words + excluded.words,
                        chars = chars + excluded.chars
                    """,
                    [(g, d, u, c, s, v[0], v[1], v[2]) for (g, d, u, c, s), v in msg.items()],
                )
            if hourly:
                self._db.executemany(
                    """
                    INSERT INTO hourly (guild_id, day, user_id, hour, src, count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id, hour, src) DO UPDATE SET count = count + excluded.count
                    """,
                    [(g, d, u, h, s, c) for (g, d, u, h, s), c in hourly.items()],
                )
            if reactions:
                self._db.executemany(
                    """
                    INSERT INTO reactions (guild_id, day, user_id, given, received)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id) DO UPDATE SET
                        given = given + excluded.given,
                        received = received + excluded.received
                    """,
                    [(g, d, u, v[0], v[1]) for (g, d, u), v in reactions.items()],
                )
            if voice:
                self._db.executemany(
                    """
                    INSERT INTO voice (guild_id, day, user_id, seconds)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id) DO UPDATE SET seconds = seconds + excluded.seconds
                    """,
                    [(g, d, u, s) for (g, d, u), s in voice.items()],
                )
            if membership:
                self._db.executemany(
                    """
                    INSERT INTO membership (guild_id, day, joins, leaves)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, day) DO UPDATE SET
                        joins = joins + excluded.joins,
                        leaves = leaves + excluded.leaves
                    """,
                    [(g, d, v[0], v[1]) for (g, d), v in membership.items()],
                )
            if meta:
                self._db.executemany(
                    "INSERT OR IGNORE INTO meta (guild_id, live_since) VALUES (?, ?)",
                    list(meta.items()),
                )
            self._db.commit()

    async def _flush_now(self) -> None:
        async with self._acc_lock:
            if not self._dirty:
                return
            snapshot = self._drain_accumulators_unsafe()
        try:
            await asyncio.to_thread(self._write_snapshot, *snapshot)
        except Exception:
            logger.exception("Stats: flush to sqlite failed, buffered counts will retry next cycle")
            async with self._acc_lock:
                self._merge_back(snapshot)
                self._dirty = True

    @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
    async def _flush_stats(self):
        await self._flush_now()

    @_flush_stats.before_loop
    async def _before_flush_stats(self):
        await self.bot.wait_until_ready()

    def _sweep_old_rows(self, cutoff: str) -> None:
        with self._db_lock:
            for table in ("messages", "hourly", "reactions", "voice", "membership"):
                self._db.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
            self._db.commit()

    @tasks.loop(hours=24)
    async def _sweep_stats(self):
        cutoff = self._day_str(datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS))
        await asyncio.to_thread(self._sweep_old_rows, cutoff)

    @_sweep_stats.before_loop
    async def _before_sweep_stats(self):
        await self.bot.wait_until_ready()

    # --- Query helpers ----------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        with self._db_lock:
            return self._db.execute(sql, params).fetchall()

    async def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        # Flush first so a query reflects the last few seconds of buffered
        # activity too, not just what was already on disk before the last
        # periodic flush — otherwise `.stats` right after a burst of
        # messages would look stale for up to FLUSH_INTERVAL_SECONDS.
        await self._flush_now()
        return await asyncio.to_thread(self._query, sql, params)

    @staticmethod
    def _day_str(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _period_start(period: str) -> str | None:
        if period == "all":
            return None
        return Stats._day_str(datetime.now(timezone.utc) - timedelta(days=_PERIOD_DAYS[period]))

    @staticmethod
    def _member_label(guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.mention if member else f"Unknown (`{user_id}`)"

    @staticmethod
    def _format_duration(seconds) -> str:
        seconds = int(seconds or 0)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"

    # --- Ingest -------------------------------------------------------------

    async def _accumulate_message(
        self, guild_id: int, day: str, hour: int, user_id: int, channel_id: int, *,
        words: int, chars: int, src: int = 0,
    ) -> None:
        """Shared by on_message and the backfill task, so both feed the same
        accumulators/flush path (backfill only ever calls this one). `src`
        distinguishes live ingest (0, the default) from backfill (1) so the
        two never merge into the same row — see the meta/live_since split in
        _run_backfill."""
        async with self._acc_lock:
            vals = self._msg_acc[(guild_id, day, user_id, channel_id, src)]
            vals[0] += 1
            vals[1] += words
            vals[2] += chars
            self._hourly_acc[(guild_id, day, user_id, hour, src)] += 1
            self._dirty = True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(message.created_at)
        hour = message.created_at.astimezone(timezone.utc).hour
        await self._accumulate_message(
            guild.id, day, hour, message.author.id, message.channel.id,
            words=len(message.content.split()), chars=len(message.content),
        )
        # Live path only — never inside _accumulate_message, which backfill
        # also calls. Records the earliest instant live ingest has seen a
        # message for this guild; _run_backfill reads it back as the
        # timestamp boundary between "seed" and "already owned by live".
        iso = message.created_at.astimezone(timezone.utc).isoformat()
        async with self._acc_lock:
            self._meta_acc.setdefault(guild.id, iso)
            self._dirty = True

    async def _handle_reaction(self, reaction: discord.Reaction, user, *, delta: int) -> None:
        message = reaction.message
        guild = message.guild
        if guild is None or user.bot:
            return
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        author = message.author
        async with self._acc_lock:
            self._reaction_acc[(guild.id, day, user.id)][0] += delta
            # A self-reaction hits the same accumulator key for both indices
            # (given[0] and received[1]) via defaultdict's identity-per-key
            # lookup — no special-casing needed for author == user.
            if author is not None and not author.bot:
                self._reaction_acc[(guild.id, day, author.id)][1] += delta
            self._dirty = True

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user) -> None:
        # Non-raw: needs reaction.message.author from discord.py's connection
        # cache without an extra API call. Limitation: only fires for
        # messages still in that cache (recent ones) — accepted trade-off to
        # avoid per-reaction fetches and rate-limit risk.
        await self._handle_reaction(reaction, user, delta=1)

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction: discord.Reaction, user) -> None:
        await self._handle_reaction(reaction, user, delta=-1)

    async def _accumulate_voice_seconds(self, guild_id: int, user_id: int, elapsed: float) -> None:
        if elapsed <= 0:
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._voice_acc[(guild_id, day, user_id)] += int(elapsed)
            self._dirty = True

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if before.channel == after.channel or member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        key = (guild.id, member.id)
        now = time.monotonic()
        if before.channel is None and after.channel is not None:
            self._voice_sessions[key] = now
            return
        # Leaving voice entirely, or moving to a different channel — either
        # way the current session ends here; credited to the end day per the
        # plan. A move immediately opens a fresh session in the new channel.
        start = self._voice_sessions.pop(key, None)
        if start is not None:
            await self._accumulate_voice_seconds(guild.id, member.id, now - start)
        if after.channel is not None:
            self._voice_sessions[key] = now

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._membership_acc[(guild.id, day)][0] += 1
            self._dirty = True

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._membership_acc[(guild.id, day)][1] += 1
            self._dirty = True

    # --- Commands -----------------------------------------------------------

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def _require_admin(self, ctx, action: str) -> bool:
        """Inline Manage-Server-or-owner gate for `stats user` on another
        member — mirrors has_permissions_or_owner's check but applied
        conditionally (only when member != ctx.author), so it can't be a
        plain decorator. `quietest`/`backfill` gate unconditionally, so they
        use the has_permissions_or_owner decorator directly instead."""
        if ctx.author.guild_permissions.manage_guild or await self.bot.is_owner(ctx.author):
            return True
        await self._reply(ctx, f"You need Manage Server to {action}.")
        return False

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "stats")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MemberNotFound):
            await self._reply(ctx, "I couldn't find that member.")
        elif isinstance(error, commands.CheckAnyFailure):
            # A CheckFailure sibling, not a MissingPermissions subclass — raised by
            # has_permissions_or_owner on stats_quietest/stats_backfill.
            await self._reply(ctx, "You don't have permission to do that.")
        elif isinstance(error, commands.CheckFailure):
            return
        elif isinstance(error, commands.MissingPermissions):
            await self._reply(ctx, "You don't have permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await self._reply(ctx, "I don't have permission to do that.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await self._reply(ctx, str(error) or "Invalid or missing argument.")
        else:
            raise error

    async def _show_server(self, ctx) -> None:
        guild = ctx.guild
        totals = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), MIN(day) FROM messages WHERE guild_id = ?",
            (guild.id,),
        )
        total_count, total_words, first_day = totals[0]

        trend_start = self._day_str(datetime.now(timezone.utc) - timedelta(days=TREND_PERIOD_DAYS))
        prior_start = self._day_str(datetime.now(timezone.utc) - timedelta(days=2 * TREND_PERIOD_DAYS))
        recent = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COUNT(DISTINCT user_id) FROM messages "
            "WHERE guild_id = ? AND day >= ?",
            (guild.id, trend_start),
        )
        recent_count, active_members = recent[0]
        prior = await self._fetch(
            "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ? AND day >= ? AND day < ?",
            (guild.id, prior_start, trend_start),
        )
        prior_count = prior[0][0]

        if prior_count:
            trend_pct = (recent_count - prior_count) / prior_count * 100
            arrow = "▲" if trend_pct >= 0 else "▼"
            trend_text = f"{arrow} {abs(trend_pct):.1f}% vs prior {TREND_PERIOD_DAYS}d"
        elif recent_count:
            trend_text = "▲ new activity (no prior-period data yet)"
        else:
            trend_text = "No recent activity"

        if first_day:
            first_date = datetime.strptime(first_day, "%Y-%m-%d").date()
            elapsed_days = max(1, (datetime.now(timezone.utc).date() - first_date).days + 1)
        else:
            elapsed_days = 1
        avg_day = total_count / elapsed_days

        reaction_total = await self._fetch(
            "SELECT COALESCE(SUM(given),0) FROM reactions WHERE guild_id = ?", (guild.id,)
        )
        voice_total = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ?", (guild.id,)
        )
        top_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id ORDER BY c DESC",
            (guild.id,),
        )
        entries = [(self._member_label(guild, uid), c) for uid, c in top_rows]

        embed = discord.Embed(title=f"📊 {guild.name} — Stats", color=discord.Color.blurple())
        embed.add_field(name="Total messages", value=f"{total_count:,}", inline=True)
        embed.add_field(name=f"Active members ({TREND_PERIOD_DAYS}d)", value=str(active_members), inline=True)
        embed.add_field(name="Trend", value=trend_text, inline=True)
        embed.add_field(
            name="Averages",
            value=f"{avg_day:.1f}/day · {avg_day * 7:.0f}/week · {avg_day * 30:.0f}/month · {avg_day * 365:.0f}/year",
            inline=False,
        )
        embed.add_field(
            name="Words / Reactions / Voice",
            value=(
                f"{total_words:,} words · {reaction_total[0][0]:,} reactions · "
                f"{self._format_duration(voice_total[0][0])}"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Top {min(TOP_N, len(entries))} posters", value=_format_distribution(entries, TOP_N), inline=False
        )
        await self._reply(ctx, embed=embed)

    @commands.hybrid_group(
        invoke_without_command=True, fallback="server", case_insensitive=True,
        description="Show server-wide message statistics.",
    )
    @commands.guild_only()
    async def stats(self, ctx):
        """Show server-wide message statistics."""
        await self._show_server(ctx)

    # with_app_command=False: the group's fallback="server" above already
    # registers the slash-side `/stats server`; this only adds the
    # prefix-side `.stats server` (typing the subcommand name explicitly),
    # mirroring palantir's status/fallback pattern.
    @stats.command(name="server", with_app_command=False)
    @commands.guild_only()
    async def stats_server(self, ctx):
        """Show server-wide message statistics."""
        await self._show_server(ctx)

    @stats.command(name="user", description="Show one member's message statistics.")
    @commands.guild_only()
    async def stats_user(self, ctx, member: discord.Member = None, n: int = TOP_N):
        """Show one member's stats — your own by default, or another member's
        (requires Manage Server). `n` controls how many of their top channels
        are shown."""
        member = member or ctx.author
        if member.id != ctx.author.id and not await self._require_admin(ctx, "view another member's stats"):
            return
        n = max(1, min(n, TOP_N_MAX))
        guild = ctx.guild

        msg_row = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), COUNT(DISTINCT day), MIN(day) "
            "FROM messages WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        total_count, total_words, active_days, first_day = msg_row[0]

        server_row = await self._fetch("SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?", (guild.id,))
        server_total = server_row[0][0]

        rank_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id", (guild.id,)
        )
        rank = rank_of(rank_rows, key=lambda kv: kv[1], target_id=member.id)

        reaction_row = await self._fetch(
            "SELECT COALESCE(SUM(given),0), COALESCE(SUM(received),0) FROM reactions "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        given, received = reaction_row[0]

        voice_row = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        voice_seconds = voice_row[0][0]

        # Busiest hour: this member's own hourly rows, summed per hour-of-day.
        busiest_hour_row = await self._fetch(
            "SELECT hour, SUM(count) as c FROM hourly WHERE guild_id = ? AND user_id = ? "
            "GROUP BY hour ORDER BY c DESC LIMIT 1",
            (guild.id, member.id),
        )
        busiest_hour = f"{busiest_hour_row[0][0]:02d}:00 UTC" if busiest_hour_row else "—"

        channel_rows = await self._fetch(
            "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ? AND user_id = ? "
            "GROUP BY channel_id ORDER BY c DESC",
            (guild.id, member.id),
        )
        channel_entries = [(f"<#{cid}>", c) for cid, c in channel_rows]

        if first_day:
            first_date = datetime.strptime(first_day, "%Y-%m-%d").date()
            elapsed_days = max(1, (datetime.now(timezone.utc).date() - first_date).days + 1)
        else:
            elapsed_days = 1
        avg_day = total_count / elapsed_days
        pct_of_server = (total_count / server_total * 100) if server_total else 0.0
        words_per_msg = (total_words / total_count) if total_count else 0.0

        embed = discord.Embed(title=f"📊 {member.display_name}'s Stats", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Total messages", value=f"{total_count:,}", inline=True)
        embed.add_field(name="Server rank", value=f"#{rank}" if rank else "Unranked", inline=True)
        embed.add_field(name="% of server", value=f"{pct_of_server:.1f}%", inline=True)
        embed.add_field(
            name="Averages", value=f"{avg_day:.1f}/day · {avg_day * 7:.0f}/week · {avg_day * 30:.0f}/month",
            inline=False,
        )
        embed.add_field(name="Active days", value=str(active_days), inline=True)
        embed.add_field(name="Busiest hour", value=busiest_hour, inline=True)
        embed.add_field(name="Words/message", value=f"{words_per_msg:.1f}", inline=True)
        embed.add_field(name="Voice time", value=self._format_duration(voice_seconds), inline=True)
        embed.add_field(name="Reactions given/received", value=f"{given:,} / {received:,}", inline=False)
        if channel_entries:
            embed.add_field(
                name=f"Top {min(n, len(channel_entries))} channels",
                value=_format_distribution(channel_entries, n),
                inline=False,
            )
        await self._reply(ctx, embed=embed)

    @stats.command(name="top", description="Show the top message posters.")
    @commands.guild_only()
    async def stats_top(self, ctx, period: PeriodConverter = "all", n: int = TOP_N):
        """Show the top-n message posters, with % share (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY c DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(self._member_label(ctx.guild, uid), c) for uid, c in rows]
        embed = discord.Embed(title=f"🏆 Top Posters — {period}", color=discord.Color.gold())
        embed.description = _format_distribution(entries, n)
        await self._reply(ctx, embed=embed)

    @stats.command(name="channels", description="Show the busiest channels.")
    @commands.guild_only()
    async def stats_channels(self, ctx, period: PeriodConverter = "all", n: int = TOP_N):
        """Show the busiest channels by message count (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY channel_id ORDER BY c DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(f"<#{cid}>", c) for cid, c in rows]
        embed = discord.Embed(title=f"📊 Busiest Channels — {period}", color=discord.Color.blurple())
        embed.description = _format_distribution(entries, n)
        await self._reply(ctx, embed=embed)

    @stats.command(name="activity", description="Show hour/weekday activity patterns.")
    @commands.guild_only()
    async def stats_activity(self, ctx, period: PeriodConverter = "month"):
        """Show peak hour/weekday activity, plus an hour x weekday heatmap image."""
        start_day = self._period_start(period)
        # Guild-wide: SUM across all users (no user_id filter), grouped by the
        # (day, hour) pair — weekday is derived from `day` below.
        sql = "SELECT day, hour, SUM(count) FROM hourly WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY day, hour"
        rows = await self._fetch(sql, tuple(params))

        grid = [[0] * 24 for _ in range(7)]
        for day, hour, count in rows:
            try:
                weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
            except ValueError:
                continue
            grid[weekday][hour] += count

        hour_totals = [sum(grid[wd][hr] for wd in range(7)) for hr in range(24)]
        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        weekday_totals = [sum(grid[wd]) for wd in range(7)]
        total = sum(hour_totals)

        if total == 0:
            await self._reply(ctx, "No message activity recorded yet for this period.")
            return

        peak_hour = max(range(24), key=lambda h: hour_totals[h])
        peak_weekday = max(range(7), key=lambda w: weekday_totals[w])
        summary = (
            f"**Peak hour:** {peak_hour:02d}:00 UTC ({hour_totals[peak_hour]:,} messages)\n"
            f"**Peak day:** {weekday_labels[peak_weekday]} ({weekday_totals[peak_weekday]:,} messages)\n"
            f"**Hourly pattern (00-23 UTC):** `{_sparkline(hour_totals)}`"
        )

        try:
            buffer = await asyncio.to_thread(_render_heatmap, grid, weekday_labels)
            file = discord.File(buffer, filename="activity_heatmap.png")
            embed = discord.Embed(
                title=f"📊 Activity — {period}", description=summary, color=discord.Color.blurple()
            )
            embed.set_image(url="attachment://activity_heatmap.png")
            await self._reply(ctx, embed=embed, file=file)
        except Exception:
            # Best-effort image: never fail the whole reply over a render problem.
            logger.exception("Stats: heatmap render failed, falling back to text summary")
            embed = discord.Embed(
                title=f"📊 Activity — {period} (image unavailable)", description=summary,
                color=discord.Color.blurple(),
            )
            await self._reply(ctx, embed=embed)

    @stats.command(name="voice", description="Show the top members by voice time.")
    @commands.guild_only()
    async def stats_voice(self, ctx, period: PeriodConverter = "all", n: int = TOP_N):
        """Show the top-n members by voice time (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT user_id, SUM(seconds) as s FROM voice WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY s DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(self._member_label(ctx.guild, uid), s) for uid, s in rows]
        embed = discord.Embed(title=f"🔊 Voice Time — {period}", color=discord.Color.teal())
        embed.description = _format_distribution(entries, n, unit=self._format_duration)
        await self._reply(ctx, embed=embed)

    @stats.command(name="growth", description="Show member joins/leaves and net growth.")
    @commands.guild_only()
    async def stats_growth(self, ctx, period: PeriodConverter = "month"):
        """Show joins/leaves/net member growth alongside message activity for
        the same period (period: week/month/year/all)."""
        start_day = self._period_start(period)
        sql = "SELECT COALESCE(SUM(joins),0), COALESCE(SUM(leaves),0) FROM membership WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        row = await self._fetch(sql, tuple(params))
        joins, leaves = row[0]

        msg_sql = "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?"
        msg_params = [ctx.guild.id]
        if start_day:
            msg_sql += " AND day >= ?"
            msg_params.append(start_day)
        msg_row = await self._fetch(msg_sql, tuple(msg_params))
        total_messages = msg_row[0][0]

        net = joins - leaves
        embed = discord.Embed(
            title=f"📈 Growth — {period}", color=discord.Color.green() if net >= 0 else discord.Color.red()
        )
        embed.add_field(name="Joins", value=f"+{joins:,}", inline=True)
        embed.add_field(name="Leaves", value=f"-{leaves:,}", inline=True)
        embed.add_field(name="Net", value=f"{net:+,}", inline=True)
        embed.add_field(
            name="Messages in period",
            value=f"{total_messages:,} — shown alongside growth, not a statistical correlation",
            inline=False,
        )
        await self._reply(ctx, embed=embed)

    @stats.command(name="quietest", description="Show the least-active members (Manage Server only).")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def stats_quietest(self, ctx, n: int = TOP_N):
        """Show the n least-active members over the last TREND_PERIOD_DAYS
        (requires Manage Server)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._day_str(datetime.now(timezone.utc) - timedelta(days=TREND_PERIOD_DAYS))
        rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day >= ? GROUP BY user_id",
            (ctx.guild.id, start_day),
        )
        counts = {uid: c for uid, c in rows}
        members = [m for m in ctx.guild.members if not m.bot]
        quietest = sorted(members, key=lambda m: counts.get(m.id, 0))[:n]
        lines = [f"{m.mention} — {counts.get(m.id, 0)} messages" for m in quietest]
        embed = discord.Embed(
            title=f"🤫 Quietest Members (last {TREND_PERIOD_DAYS}d)",
            description="\n".join(lines) or "No members found.",
            color=discord.Color.dark_grey(),
        )
        await self._reply(ctx, embed=embed)

    @stats.command(
        name="backfill",
        description="Seed historical stats from channel logs (Manage Server only). No days = entire history.",
    )
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def stats_backfill(self, ctx, days: int = BACKFILL_DEFAULT_DAYS):
        """Seed message/word/char/hourly history from every readable message
        source (text/announcement channels, threads — active and archived,
        including forum posts — and voice/stage channel text chat). No `days`
        = the entire server history (can take a while on a large server, but
        discord.py auto-throttles pagination so it can't outrun the rate
        limit). Only seeds messages/words/chars/hourly — reactions/voice
        can't be recovered retroactively. Seeds everything strictly before
        the instant collection started for this server (never anything at or
        after — that's live ingest's territory), so the partial startup day
        is captured too. Idempotent: re-running only replaces its own
        previously-seeded rows, never live ones, so it's safe to run again
        (e.g. after inviting the bot to more channels)."""
        guild = ctx.guild
        if guild.id in self._backfill_in_progress:
            await self._reply(ctx, "A backfill is already running for this server — wait for it to finish.")
            return
        self._backfill_in_progress.add(guild.id)

        days = max(0, days)
        if days > 0:
            after = datetime.now(timezone.utc) - timedelta(days=days)
            scope = f"the last {days} day(s)"
        else:
            after = None
            scope = "the entire server history"

        await self._reply(
            ctx,
            f"⏳ Backfill started for {scope}. This scans every readable channel's message "
            "history and can take a while on a large server — I'll post a summary here when done.",
        )
        asyncio.ensure_future(self._run_backfill(guild, ctx.channel, after))

    async def _backfill_sources(self, guild: discord.Guild):
        """Yield every history-capable messageable object that live ingest's
        on_message can fire for: text/announcement channels, voice/stage
        channel text chat, and threads (active + archived, public + private,
        including forum posts) attached to a text channel or forum. Bare
        objects only — permission checks and history iteration are the
        caller's job, so one bad source doesn't abort the whole scan."""
        for channel in guild.text_channels:
            yield channel
        for channel in guild.voice_channels:
            yield channel
        for channel in guild.stage_channels:
            yield channel

        # Forums have no messages of their own — only their threads (posts)
        # do — so they're scanned here as thread parents, not as sources above.
        for parent in list(guild.text_channels) + list(guild.forums):
            for thread in parent.threads:
                yield thread
            try:
                async for thread in parent.archived_threads(limit=None):
                    yield thread
            except (discord.Forbidden, discord.HTTPException):
                pass
            # Private archived threads: only TextChannel supports the
            # `private=` kwarg (ForumChannel threads have no private variant).
            if isinstance(parent, discord.TextChannel):
                try:
                    async for thread in parent.archived_threads(limit=None, private=True):
                        yield thread
                except (discord.Forbidden, discord.HTTPException):
                    pass

    def _ensure_live_since(self, guild_id: int, now_iso: str) -> str:
        """Blocking: seed `meta.live_since` with `now_iso` if this guild has
        none yet (INSERT OR IGNORE — a no-op if on_message already set one),
        then read back the effective value. Seeding on first-ever backfill
        (before any live message) means backfill owns everything before this
        moment and live ingest owns everything from it forward."""
        with self._db_lock:
            self._db.execute(
                "INSERT OR IGNORE INTO meta (guild_id, live_since) VALUES (?, ?)",
                (guild_id, now_iso),
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT live_since FROM meta WHERE guild_id=?", (guild_id,)
            ).fetchone()
            return row[0]

    def _delete_backfill_range(self, guild_id: int, after_day: str | None) -> None:
        """Blocking: delete only backfill-sourced (src=1) rows for this guild
        so a re-run reseeds cleanly instead of piling on top of last time.
        Live rows (src=0) are a separate PRIMARY KEY dimension, so this never
        touches them — including on the boundary day, which can hold both."""
        with self._db_lock:
            for table in ("messages", "hourly"):
                if after_day is not None:
                    self._db.execute(
                        f"DELETE FROM {table} WHERE guild_id=? AND src=1 AND day>=?",
                        (guild_id, after_day),
                    )
                else:
                    self._db.execute(
                        f"DELETE FROM {table} WHERE guild_id=? AND src=1",
                        (guild_id,),
                    )
            self._db.commit()

    async def _run_backfill(self, guild: discord.Guild, report_channel, after: datetime | None) -> None:
        started = time.monotonic()
        sources_scanned = 0
        sources_skipped = 0
        messages_seen = 0
        try:
            # Flush first so the ensure/read below reflects the last few
            # seconds of buffered activity too, then delete-then-reseed the
            # backfill's own range (src=1 only, never touches live rows) so a
            # re-run is idempotent instead of piling on top of last time.
            await self._flush_now()
            now_iso = discord.utils.utcnow().isoformat()
            live_since_str = await asyncio.to_thread(self._ensure_live_since, guild.id, now_iso)
            live_since_dt = datetime.fromisoformat(live_since_str)

            after_day = self._day_str(after) if after is not None else None
            await asyncio.to_thread(self._delete_backfill_range, guild.id, after_day)

            async for source in self._backfill_sources(guild):
                perms = source.permissions_for(guild.me)
                if not perms.view_channel or not perms.read_message_history:
                    sources_skipped += 1
                    continue
                try:
                    async for message in source.history(limit=None, after=after):
                        if message.author.bot:
                            continue
                        if message.created_at >= live_since_dt:
                            continue  # at/after collection start — owned by live ingest
                        day = self._day_str(message.created_at)
                        hour = message.created_at.astimezone(timezone.utc).hour
                        await self._accumulate_message(
                            guild.id, day, hour, message.author.id, source.id,
                            words=len(message.content.split()), chars=len(message.content),
                            src=1,
                        )
                        messages_seen += 1
                except (discord.Forbidden, discord.HTTPException):
                    sources_skipped += 1
                    continue
                sources_scanned += 1
                await asyncio.sleep(BACKFILL_CHANNEL_SLEEP)

            await self._flush_now()
            elapsed = time.monotonic() - started
            await report_channel.send(
                f"✅ Backfill complete: scanned {sources_scanned} source(s) — channels, threads/forum "
                f"posts, voice text ({sources_skipped} skipped), recorded {messages_seen:,} message(s) "
                f"in {elapsed:.0f}s."
            )
        except Exception:
            logger.exception("Stats: backfill failed for guild %s", guild.id)
            try:
                await report_channel.send("⚠️ Backfill hit an unexpected error and stopped early — check the logs.")
            except discord.HTTPException:
                pass
        finally:
            self._backfill_in_progress.discard(guild.id)

    @stats.command(
        name="reset",
        description="Erase this server's stats entirely (Manage Server only). Requires confirm:confirm.",
    )
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def stats_reset(self, ctx, confirm: str = None):
        """Wipe every message/hour/reaction/voice/membership/meta row for
        this server. Destructive and irreversible, so it requires the
        literal argument `confirm` (`.stats reset confirm` /
        `/stats reset confirm:confirm`) — anything else just shows this
        warning without touching data. Unlike the rest of the bot's commands,
        this token is deliberately case-sensitive: it's a speed bump on an
        irreversible six-table per-guild wipe, not a case-insensitivity gap
        to "fix"."""
        # Deliberately exact-match, not .lower()'d — see the docstring above.
        if confirm != "confirm":
            await self._reply(
                ctx,
                "⚠️ This permanently erases **all** stats for this server — messages, words/chars, "
                "hourly activity, reactions, voice time, joins/leaves, and backfill history. This "
                "cannot be undone. To proceed, run `.stats reset confirm` (or `/stats reset "
                "confirm:confirm`).",
            )
            return
        await self._reset_guild(ctx.guild.id)
        await self._reply(ctx, "🗑️ This server's stats have been wiped.")

    async def _reset_guild(self, guild_id: int) -> None:
        """Drop every trace of one guild's stats, in-memory and on disk.
        Accumulators are cleared first (under _acc_lock) so nothing buffered
        right now can repopulate the guild's rows right after the DB wipe."""
        async with self._acc_lock:
            for acc in (self._msg_acc, self._hourly_acc, self._reaction_acc, self._voice_acc):
                for key in [k for k in acc if k[0] == guild_id]:
                    del acc[key]
            for key in [k for k in self._membership_acc if k[0] == guild_id]:
                del self._membership_acc[key]
            self._meta_acc.pop(guild_id, None)
            for key in [k for k in self._voice_sessions if k[0] == guild_id]:
                del self._voice_sessions[key]
            self._backfill_in_progress.discard(guild_id)

        await asyncio.to_thread(self._delete_guild_rows, guild_id)

    def _delete_guild_rows(self, guild_id: int) -> None:
        with self._db_lock:
            for table in ("messages", "hourly", "reactions", "voice", "membership", "meta"):
                self._db.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
            self._db.commit()

    # --- Wrapped ------------------------------------------------------------

    async def _wrapped_year(self, ctx, year: int | None) -> int | None:
        """Shared preflight for both Wrapped commands: the lines loaded and
        `year` (default: current Berlin-time year) is in range. Returns the year, or
        None after replying."""
        if self._wrapped_lines is None:
            await self._reply(ctx, "Kurisu Wrapped ist gerade nicht verfügbar.")
            return None
        current = datetime.now(WRAPPED_TZ).year
        year = current if year is None else year
        if not WRAPPED_MIN_YEAR <= year <= current:
            await self._reply(ctx, f"Wrapped gibt es nur für die Jahre {WRAPPED_MIN_YEAR} bis {current}.")
            return None
        return year

    @staticmethod
    async def _read_asset(asset) -> bytes | None:
        """Avatar/guild-icon bytes for the card, or None — best-effort, a
        failed CDN fetch just means the card gets a lettered placeholder."""
        if asset is None:
            return None
        try:
            return await asset.replace(size=256).read()
        except Exception:
            logger.warning("Stats: couldn't fetch an image for the Wrapped card", exc_info=True)
            return None

    @staticmethod
    async def _render_wrapped_png(data: dict) -> bytes | None:
        try:
            buffer = await asyncio.to_thread(_render_wrapped_card, data)
            return buffer.getvalue()
        except Exception:
            # Best-effort image, same as the heatmap: the last page shows the
            # highlights as text instead.
            logger.exception("Stats: Wrapped card render failed, falling back to text highlights")
            return None

    @staticmethod
    def _channel_name(guild: discord.Guild, channel_id: int) -> str:
        channel = guild.get_channel_or_thread(channel_id)
        return f"#{channel.name}" if channel else "#unbekannt"

    async def _gather_user_wrapped(self, guild: discord.Guild, member: discord.Member, year: int) -> dict:
        """Everything the personal story shows, for one calendar year (UTC
        days). Rank is among members with at least one message that year."""
        window = (f"{year}-01-01", f"{year}-12-31")
        g, u = guild.id, member.id

        rank_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day BETWEEN ? AND ? "
            "GROUP BY user_id HAVING c > 0",
            (g, *window),
        )
        rank = rank_of(rank_rows, key=lambda kv: kv[1], target_id=u)
        active_users = len(rank_rows)

        msg_row = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0) FROM messages "
            "WHERE guild_id = ? AND user_id = ? AND day BETWEEN ? AND ?",
            (g, u, *window),
        )
        messages, words = msg_row[0]

        day_rows = await self._fetch(
            "SELECT DISTINCT day FROM messages WHERE guild_id = ? AND user_id = ? AND day BETWEEN ? AND ?",
            (g, u, *window),
        )
        days = [d for (d,) in day_rows]

        channel_rows = await self._fetch(
            "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ? AND user_id = ? "
            "AND day BETWEEN ? AND ? GROUP BY channel_id HAVING c > 0 ORDER BY c DESC",
            (g, u, *window),
        )
        channels = [
            {
                "mention": f"<#{cid}>", "name": self._channel_name(guild, cid),
                "count": c, "share": c / messages if messages else 0.0,
            }
            for cid, c in channel_rows
        ]

        month_rows = await self._fetch(
            "SELECT CAST(substr(day, 6, 2) AS INTEGER), SUM(count) FROM messages "
            "WHERE guild_id = ? AND user_id = ? AND day BETWEEN ? AND ? GROUP BY 1",
            (g, u, *window),
        )
        monthly = [0] * 12
        for month, count in month_rows:
            if 1 <= month <= 12:
                monthly[month - 1] = count

        # Raw (day, hour) rows, not pre-grouped by hour: each one is converted
        # to Berlin time on its own date, so DST is applied per day.
        hourly_rows = await self._fetch(
            "SELECT day, hour, SUM(count) FROM hourly WHERE guild_id = ? AND user_id = ? "
            "AND day BETWEEN ? AND ? GROUP BY day, hour",
            (g, u, *window),
        )
        hour_totals, weekday_totals = _local_activity(hourly_rows)

        voice_row = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ? AND user_id = ? AND day BETWEEN ? AND ?",
            (g, u, *window),
        )
        reaction_row = await self._fetch(
            "SELECT COALESCE(SUM(given),0), COALESCE(SUM(received),0) FROM reactions "
            "WHERE guild_id = ? AND user_id = ? AND day BETWEEN ? AND ?",
            (g, u, *window),
        )
        # Clamped: a reaction removed on a later day than it was added nets
        # a negative day row, which could tip a tiny total below zero.
        given, received = (max(0, v) for v in reaction_row[0])

        return {
            "kind": "user",
            "year": year,
            "partial": year == datetime.now(WRAPPED_TZ).year,
            "title": _wrapped_title(year, datetime.now(WRAPPED_TZ).year),
            "name": member.display_name,
            "card_name": member.display_name,
            "icon_url": member.display_avatar.url,
            "messages": messages,
            "words_per_msg": words / messages if messages else 0.0,
            "rank": rank,
            "active_users": active_users,
            "percent": _top_percent(rank, active_users),
            "channels": channels,
            "monthly": monthly,
            "busiest_month": _peak(monthly),
            "active_days": len(days),
            "streak": _longest_streak(days),
            "busiest_hour": _peak(hour_totals),
            "busiest_weekday": _peak(weekday_totals),
            "voice_seconds": max(0, voice_row[0][0]),
            "given": given,
            "received": received,
        }

    async def _gather_server_wrapped(self, guild: discord.Guild, year: int) -> dict:
        """Everything the server story shows, for one calendar year (UTC days)."""
        window = (f"{year}-01-01", f"{year}-12-31")
        g = guild.id

        totals = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COUNT(DISTINCT user_id) FROM messages "
            "WHERE guild_id = ? AND day BETWEEN ? AND ?",
            (g, *window),
        )
        messages, members = totals[0]

        user_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day BETWEEN ? AND ? "
            "GROUP BY user_id ORDER BY c DESC LIMIT ?",
            (g, *window, TOP_N),
        )
        top_users = []
        for uid, c in user_rows:
            m = guild.get_member(uid)
            top_users.append({
                "mention": m.mention if m else "Unbekannt",
                "name": m.display_name if m else "Unbekannt",
                "count": c,
            })

        channel_rows = await self._fetch(
            "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day BETWEEN ? AND ? "
            "GROUP BY channel_id ORDER BY c DESC LIMIT ?",
            (g, *window, TOP_N),
        )
        top_channels = [
            {"mention": f"<#{cid}>", "name": self._channel_name(guild, cid), "count": c} for cid, c in channel_rows
        ]

        month_rows = await self._fetch(
            "SELECT CAST(substr(day, 6, 2) AS INTEGER), SUM(count) FROM messages "
            "WHERE guild_id = ? AND day BETWEEN ? AND ? GROUP BY 1",
            (g, *window),
        )
        monthly = [0] * 12
        for month, count in month_rows:
            if 1 <= month <= 12:
                monthly[month - 1] = count

        day_rows = await self._fetch(
            "SELECT day, SUM(count) as c FROM messages WHERE guild_id = ? AND day BETWEEN ? AND ? "
            "GROUP BY day ORDER BY c DESC, day LIMIT 1",
            (g, *window),
        )
        busiest_day, busiest_day_count = day_rows[0] if day_rows else (None, 0)

        growth = await self._fetch(
            "SELECT COALESCE(SUM(joins),0), COALESCE(SUM(leaves),0) FROM membership "
            "WHERE guild_id = ? AND day BETWEEN ? AND ?",
            (g, *window),
        )
        joins, leaves = growth[0]

        return {
            "kind": "server",
            "year": year,
            "partial": year == datetime.now(WRAPPED_TZ).year,
            "title": _wrapped_title(year, datetime.now(WRAPPED_TZ).year),
            "name": guild.name,
            "card_name": guild.name,
            "icon_url": guild.icon.url if guild.icon else None,
            "messages": messages,
            "members": members,
            "top_users": top_users,
            "top_channels": top_channels,
            "monthly": monthly,
            "busiest_month": _peak(monthly),
            "busiest_day": busiest_day,
            "busiest_day_count": busiest_day_count,
            "joins": joins,
            "leaves": leaves,
            "net": joins - leaves,
        }

    async def _send_wrapped(self, ctx, pages: list[discord.Embed], card_png: bytes | None,
                            share_content: str) -> None:
        # The share button only exists on a slash invocation: there the story
        # is ephemeral, while a `.` invocation is already public.
        view = WrappedView(pages, ctx.author.id, card_png, share_content if ctx.interaction else None)
        view.message = await self._reply(
            ctx, embed=pages[0], view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @stats.command(name="wrapped", description="Kurisu Wrapped: your year on this server as a 5-page story.")
    @commands.guild_only()
    async def stats_wrapped(self, ctx, member: discord.Member | None = None, year: int = None):
        """Kurisu Wrapped — a member's year (default: the current one) as a
        five-page story ending in an image card. Your own for everyone;
        another member's requires Manage Server. German text, times in
        Europe/Berlin."""
        member = member or ctx.author
        if member.id != ctx.author.id and not await self._require_admin(ctx, "view another member's Wrapped"):
            return
        year = await self._wrapped_year(ctx, year)
        if year is None:
            return
        lines = self._wrapped_lines

        # Defers a slash invocation (ephemeral, matching _reply) before the
        # queries/avatar fetch/render can outlast Discord's 3s window; on
        # prefix it's just a typing indicator.
        async with ctx.typing(ephemeral=True):
            data = await self._gather_user_wrapped(ctx.guild, member, year)
            empty = not data["messages"] and not data["voice_seconds"] and not (data["given"] or data["received"])
            if not empty:
                data["avatar"] = await self._read_asset(member.display_avatar)
                card_png = await self._render_wrapped_png(data)

        if empty:
            embed = discord.Embed(
                title=data["title"], description=_pick_line(lines["user"]["empty"], {"year": year}),
                color=WRAPPED_COLOR,
            )
            await self._reply(ctx, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        pages = _build_user_pages(data, lines, card_png is not None)
        await self._send_wrapped(ctx, pages, card_png, lines["share"].format(year=year, user=member.mention))

    @stats.command(name="serverwrapped", description="Kurisu Wrapped for the whole server as a 5-page story.")
    @commands.guild_only()
    async def stats_serverwrapped(self, ctx, year: int = None):
        """Kurisu Wrapped for the whole server — a year (default: the current
        one) as a five-page story ending in an image card."""
        year = await self._wrapped_year(ctx, year)
        if year is None:
            return
        lines = self._wrapped_lines
        guild = ctx.guild

        async with ctx.typing(ephemeral=True):
            data = await self._gather_server_wrapped(guild, year)
            if data["messages"]:
                data["avatar"] = await self._read_asset(guild.icon)
                card_png = await self._render_wrapped_png(data)

        if not data["messages"]:
            embed = discord.Embed(
                title=data["title"], description=_pick_line(lines["server"]["empty"], {"year": year}),
                color=WRAPPED_COLOR,
            )
            await self._reply(ctx, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        pages = _build_server_pages(data, lines, card_png is not None)
        await self._send_wrapped(ctx, pages, card_png, lines["share"].format(year=year, user=guild.name))


async def setup(bot):
    await bot.add_cog(Stats(bot))
