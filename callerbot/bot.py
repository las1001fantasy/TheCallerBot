"""CallerBot: anuncia en Telegram quién está on the clock en los drafts de Fleaflicker.

Cada canal (un grupo, o un tema dentro de un grupo con temas) tiene como mucho una liga.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from telegram.error import RetryAfter
from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .fleaflicker import (
    SPORTS, DraftState, FleaflickerError, Team, _fmt, detect_sport, fetch_roster, fetch_scoreboard, fetch_standings,
    fetch_state, roster_issues,
)
from .i18n import LANG_WORDS, t
from .storage import Storage

log = logging.getLogger("callerbot")

# Horas de silencio: no hay recordatorios ni avisos al commish, pero sí se anuncia un nuevo OTC si alguien hace un pick.
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Madrid"))
QUIET_START = int(os.environ.get("QUIET_START", "0"))
QUIET_END = int(os.environ.get("QUIET_END", "8"))


def quiet_now() -> bool:
    hour = datetime.now(TIMEZONE).hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= hour < QUIET_END
    return hour >= QUIET_START or hour < QUIET_END


def active_seconds(start: float, end: float) -> float:
    """Segundos entre start y end sin contar las horas de silencio (el reloj del OTC se para de noche)."""
    if end <= start:
        return 0.0
    total = end - start
    day = datetime.fromtimestamp(start, TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    last = datetime.fromtimestamp(end, TIMEZONE)
    while day <= last:
        if QUIET_START <= QUIET_END:
            windows = [(day.replace(hour=QUIET_START), day.replace(hour=QUIET_END) if QUIET_END < 24 else day + timedelta(days=1))]
        else:
            windows = [(day.replace(hour=QUIET_START), (day + timedelta(days=1)).replace(hour=QUIET_END))]
        for q0, q1 in windows:
            overlap = min(end, q1.timestamp()) - max(start, q0.timestamp())
            if overlap > 0:
                total -= overlap
        day += timedelta(days=1)
    return max(total, 0.0)


OTC_TEXT = re.compile(r"who'?s on the clock|qui[eé]n est[aá] on the clock|qui est[aà] on the clock", re.IGNORECASE)


# ---------- Utilidades ----------

def storage(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.bot_data["storage"]


def http(ctx: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    return ctx.bot_data["http"]


def channel(update: Update) -> tuple[int, int]:
    """(chat_id, thread_key): el tema cuenta como canal propio en grupos con temas."""
    msg = update.effective_message
    thread = msg.message_thread_id if msg and msg.is_topic_message else 0
    return update.effective_chat.id, thread or 0


def norm(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def mention(nick: str) -> str:
    return nick if nick.startswith("@") or " " in nick else f"@{nick}"


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


MAX_MESSAGE = 4000  # Telegram corta en 4096 caracteres y rechaza el mensaje entero si se pasa


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Trocea por líneas para que un listado largo (p. ej. /getIllegalRosters) no se pierda por pasarse del límite."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [text]


async def reply(update: Update, text: str) -> None:
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        if i:
            await asyncio.sleep(1)  # varios trozos seguidos: sin pausa Telegram aplica el control de flood
        for attempt in range(3):
            try:
                await update.effective_message.reply_text(chunk, do_quote=False, link_preview_options=NO_PREVIEW)
                break
            except RetryAfter as e:  # Telegram pide esperar unos segundos: esperamos y reintentamos
                if attempt == 2:
                    raise
                wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else e.retry_after
                await asyncio.sleep(float(wait) + 1)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Que un fallo inesperado quede en los registros y el usuario no se quede esperando sin respuesta."""
    log.error("Error atendiendo un mensaje", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"⚠️ Error: {ctx.error}", do_quote=False)
        except Exception:
            pass


async def is_manager(update: Update, league) -> bool:
    """El commish de la liga, un admin del grupo, un dueño del bot, o cualquiera en un chat privado."""
    chat, user = update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return True
    # Un admin anónimo escribe "como el grupo": Telegram pone el propio grupo como remitente y solo los admins pueden hacerlo.
    msg = update.effective_message
    if msg and msg.sender_chat and msg.sender_chat.id == chat.id:
        return True
    # Los dueños del bot (variable BOT_OWNERS, p. ej. "@bocamolls @otro") pueden gestionar cualquier liga.
    owners = {norm(mention(o)) for o in os.environ.get("BOT_OWNERS", "").replace(",", " ").split()}
    if user.username and norm(f"@{user.username}") in owners:
        return True
    if league and league["commish"] and user.username and norm(league["commish"]) == norm(f"@{user.username}"):
        return True
    member = await chat.get_member(user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def lang_of(league) -> str:
    return (league["lang"] if league and "lang" in league.keys() else None) or "es"


async def require_league(update: Update, ctx: ContextTypes.DEFAULT_TYPE, manager: bool):
    league = storage(ctx).get_league(*channel(update))
    if not league:
        await reply(update, t("es", "no_league"))
        return None
    if manager and not await is_manager(update, league):
        await reply(update, t(lang_of(league), "only_manager"))
        return None
    return league


async def load_state(ctx: ContextTypes.DEFAULT_TYPE, league) -> DraftState:
    return await fetch_state(http(ctx), league["league_id"], league["sport"])


def telegram_of(team: Team, users: dict[str, str]) -> list[str]:
    found = [mention(users[norm(o)]) for o in team.owners if norm(o) in users]
    if not found and norm(team.name) in users:
        found = [mention(users[norm(team.name)])]
    return found


def who(team: Team, users: dict[str, str]) -> str:
    return " ".join(telegram_of(team, users)) or team.name


def otc_line(state: DraftState, users: dict[str, str], lang: str = "es") -> str:
    pick = state.on_the_clock
    if pick is None:
        return t(lang, "draft_over")
    return t(lang, "otc_line", who=who(pick.team, users), round=pick.round, slot=pick.slot, overall=pick.overall)


def hours_arg(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    if len(ctx.args) == 1 and ctx.args[0].isdigit() and 1 <= int(ctx.args[0]) <= 23:
        return int(ctx.args[0])
    return None


# ---------- Comandos públicos ----------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, t(lang_of(storage(ctx).get_league(*channel(update))), "help"))


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, t(lang_of(storage(ctx).get_league(*channel(update))), "ping"))


async def cmd_getchannelid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, thread = channel(update)
    lang = lang_of(storage(ctx).get_league(chat_id, thread))
    await reply(update, t(lang, "channel_id", chat_id=chat_id) + (t(lang, "topic", thread=thread) if thread else ""))


async def cmd_setleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    # El idioma va al final (CAT = catalán); sin nada, castellano.
    lang = next((LANG_WORDS[a.upper()] for a in args[1:] if a.upper() in LANG_WORDS), "es")
    rest = [a for a in args[1:] if a.upper() not in LANG_WORDS]
    if not args or not args[0].isdigit():
        await reply(update, t(lang, "setleague_usage"))
        return
    current = storage(ctx).get_league(*channel(update))
    if current and not await is_manager(update, current):
        await reply(update, t(lang_of(current), "already_set"))
        return

    league_id = int(args[0])
    commish = mention(rest[0]) if rest and rest[0].upper() not in SPORTS else None
    sport = next((a.upper() for a in rest if a.upper() in SPORTS), None)
    try:
        sport = sport or await detect_sport(http(ctx), league_id)
        state = await fetch_state(http(ctx), league_id, sport)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang, "cant_read", error=e))
        return

    storage(ctx).set_league(*channel(update), league_id, sport, commish, lang)
    await reply(update, t(lang, "league_set", league_id=league_id, sport=sport, teams=len(state.teams),
                          commish=t(lang, "commish_part", commish=commish) if commish else ""))


async def cmd_getteams(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))
        return
    lang = lang_of(league)
    users = storage(ctx).users(*channel(update))
    lines = [t(lang, "teams_header", league_id=league["league_id"])]
    for i, team in enumerate(state.teams, 1):
        owners = ", ".join(team.owners) or t(lang, "no_owner")
        tg = ", ".join(telegram_of(team, users)) or t(lang, "no_telegram")
        lines.append(f"{team.slot or i}. {team.name} · {owners} · {tg}")
    await reply(update, "\n".join(lines))


async def cmd_whosotc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = storage(ctx).get_league(*channel(update))
    if not league:
        if update.effective_message.text.startswith("/"):
            await reply(update, t("es", "no_league"))
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))
        return
    await reply(update, otc_line(state, storage(ctx).users(*channel(update)), lang_of(league)))


async def cmd_getillegalrosters(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))
        return
    lang = lang_of(league)
    await reply(update, t(lang, "checking_rosters"))
    week = None
    if (league["sport"] or "").upper() == "NFL":  # los byes solo existen en la NFL
        try:
            week = (await fetch_scoreboard(http(ctx), league["league_id"], league["sport"])).week
        except (FleaflickerError, httpx.HTTPError, ValueError):
            log.warning("No pude saber la semana actual de la liga %s para los byes", league["league_id"])

    async def one(team: Team):
        try:
            data = await fetch_roster(http(ctx), league["league_id"], league["sport"], team.id)
            if not data.get("groups"):
                log.warning("FetchRoster sin 'groups' para el equipo %s: claves %s", team.id, list(data))
            return team, roster_issues(data, lang, week), None
        except (FleaflickerError, httpx.HTTPError, ValueError) as e:
            return team, [], str(e)
        except Exception as e:  # un formato raro de un equipo no debe dejar sin respuesta al resto
            log.exception("Error revisando la plantilla del equipo %s de la liga %s", team.id, league["league_id"])
            return team, [], f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*(one(t) for t in state.teams))
    users = storage(ctx).users(*channel(update))
    lines, failed = [], []
    for team, issues, error in results:
        # El @nick de Telegram hace que al manager le llegue el aviso; si no está asociado, su usuario de Fleaflicker.
        owner = " ".join(telegram_of(team, users)) or ", ".join(team.owners) or t(lang, "no_owner")
        if error:
            failed.append(f"{team.name}: {error}")
        elif issues:
            lines.append(f"• {team.name} ({owner}):\n  - " + "\n  - ".join(issues))
    text = "\n".join([t(lang, "rosters_header"), *lines]) if lines else t(lang, "rosters_ok")
    if failed:
        text += "\n\n" + t(lang, "rosters_failed") + "\n" + "\n".join(failed)
    await reply(update, text)


# ---------- Gestión de la liga ----------

async def cmd_getleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if league:
        await reply(update, t(lang_of(league), "league_short", league_id=league["league_id"], sport=league["sport"]))


async def cmd_getleagueinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))
        return
    lang = lang_of(league)
    users = storage(ctx).users(*channel(update))
    pick = state.on_the_clock
    otc = f"{who(pick.team, users)} (#{pick.overall})" if pick else t(lang, "draft_finished_short")
    await reply(update, t(
        lang, "league_info", league_id=league["league_id"], sport=league["sport"],
        commish=league["commish"] or t(lang, "unassigned"), teams=len(state.teams), picks=state.picks_made,
        otc=otc, alerts=t(lang, "alerts_on" if league["drafting"] else "alerts_off"),
        reminder=league["reminder_hours"], notify=league["notify_hours"],
    ))


async def cmd_getmissingusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))
        return
    users = storage(ctx).users(*channel(update))
    missing = [f"{o} ({t.name})" for t in state.teams for o in t.owners if norm(o) not in users]
    orphan = [t.name for t in state.teams if not t.owners and norm(t.name) not in users]
    lang = lang_of(league)
    if not missing and not orphan:
        await reply(update, t(lang, "all_mapped"))
        return
    lines = [t(lang, "missing_header"), *missing] if missing else []
    if orphan:
        lines += [t(lang, "orphan_header"), *orphan]
    await reply(update, "\n".join(lines))


async def cmd_unsetleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if league:
        storage(ctx).unset_league(*channel(update))
        await reply(update, t(lang_of(league), "unset", league_id=league["league_id"]))


async def cmd_setcommish(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    if len(ctx.args) != 1:
        await reply(update, t(lang_of(league), "setcommish_usage"))
        return
    commish = mention(ctx.args[0])
    storage(ctx).update_league(*channel(update), commish=commish)
    await reply(update, t(lang_of(league), "commish_set", commish=commish))


async def cmd_addtelegramusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    added, bad = [], []
    for token in ctx.args:
        ff, sep, tg = token.partition(".@")
        if sep:
            tg = "@" + tg
        else:
            ff, sep, tg = token.rpartition(".")
        if not ff or not tg:
            bad.append(token)
            continue
        storage(ctx).add_user(*channel(update), norm(ff), tg)
        added.append(f"{ff} → {tg}")
    if not added and not bad:
        await reply(update, t(lang_of(league), "add_usage"))
        return
    lang = lang_of(league)
    lines = ([t(lang, "added"), *added] if added else []) + ([t(lang, "not_understood"), *bad] if bad else [])
    await reply(update, "\n".join(lines))


async def cmd_startdraft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    storage(ctx).update_league(*channel(update), drafting=1, last_overall=None, commish_notified=0)
    await reply(update, t(lang_of(league), "draft_started"))
    await check_league(ctx, storage(ctx).get_league(*channel(update)))


async def cmd_stopdraft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if league:
        storage(ctx).update_league(*channel(update), drafting=0)
        await reply(update, t(lang_of(league), "draft_stopped"))


async def cmd_setreminderduration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    hours = hours_arg(ctx)
    if hours is None:
        await reply(update, t(lang_of(league), "reminder_usage"))
        return
    storage(ctx).update_league(*channel(update), reminder_hours=hours)
    await reply(update, t(lang_of(league), "reminder_set", hours=hours))


async def cmd_setnotificationduration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    hours = hours_arg(ctx)
    if hours is None:
        await reply(update, t(lang_of(league), "notify_usage"))
        return
    storage(ctx).update_league(*channel(update), notify_hours=hours)
    await reply(update, t(lang_of(league), "notify_set", hours=hours))


# ---------- Clasificación y resultados ----------

async def standings_text(ctx: ContextTypes.DEFAULT_TYPE, league) -> str:
    lang = lang_of(league)
    rows = await fetch_standings(http(ctx), league["league_id"], league["sport"])
    lines = [t(lang, "standings_header", league_id=league["league_id"])]
    division = None
    multi = len({r.division for r in rows}) > 1
    for i, r in enumerate(sorted(rows, key=lambda r: (r.division, r.rank or 999)) if multi else rows, 1):
        if multi and r.division != division:
            division = r.division
            lines.append(f"\n{division}")
        record = f"{r.wins}-{r.losses}" + (f"-{r.ties}" if r.ties else "")
        lines.append(f"{r.rank or i}. {r.team.name} · {record} · PF {_fmt(r.points_for)} · PA {_fmt(r.points_against)}")
    return "\n".join(lines)


async def scoreboard_text(ctx: ContextTypes.DEFAULT_TYPE, league, week: int | None = None) -> str:
    lang = lang_of(league)
    board = await fetch_scoreboard(http(ctx), league["league_id"], league["sport"], week)
    header = t(lang, "scoreboard_header", week=board.week) if board.week else t(lang, "scoreboard_header_noweek")
    lines = [header]
    for g in board.games:
        status = "" if g.final else f" ({t(lang, 'in_progress')})"
        lines.append(f"{g.away.name} {_fmt(g.away_score)} - {_fmt(g.home_score)} {g.home.name}{status}")
    return "\n".join(lines)


async def cmd_getleaguestanding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        await reply(update, await standings_text(ctx, league))
    except (FleaflickerError, httpx.HTTPError, ValueError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))


async def cmd_getscoreboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    if ctx.args and not ctx.args[0].isdigit():
        await reply(update, t(lang_of(league), "scoreboard_usage"))
        return
    week = int(ctx.args[0]) if ctx.args else None
    try:
        await reply(update, await scoreboard_text(ctx, league, week))
    except (FleaflickerError, httpx.HTTPError, ValueError) as e:
        await reply(update, t(lang_of(league), "ff_error", error=e))


REPORT_DAY = 1    # martes (lunes = 0)
REPORT_HOUR = 12
REPORT_PARALLEL = 4  # informes que se preparan a la vez


def report_when(league) -> str:
    return t(lang_of(league), "when_report")


def _toggle(field: str, value: int, key: str):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        league = await require_league(update, ctx, manager=True)
        if league:
            storage(ctx).update_league(*channel(update), **{field: value})
            await reply(update, t(lang_of(league), key, when=report_when(league)))
    return handler


cmd_enablestanding = _toggle("standings_weekly", 1, "standings_on")
cmd_disablestanding = _toggle("standings_weekly", 0, "standings_off")
cmd_enablescoreboard = _toggle("scoreboard_weekly", 1, "scoreboard_on")
cmd_disablescoreboard = _toggle("scoreboard_weekly", 0, "scoreboard_off")


async def weekly_reports(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Martes a las 12h (hora de España): resultados de la jornada pasada y clasificación.

    Se lanza cada 15 minutos: las ligas se procesan unas pocas a la vez y con una pausa entre envíos, y la que
    falle (Fleaflicker lento, por ejemplo) se reintenta en la siguiente vuelta hasta que acabe el martes.
    """
    now = datetime.now(TIMEZONE)
    if now.weekday() != REPORT_DAY or now.hour < REPORT_HOUR:
        return
    year, isoweek, _ = now.isocalendar()
    tag = f"{year}-W{isoweek:02d}"
    db = storage(ctx)
    pending = [lg for lg in db.report_leagues() if lg["last_report"] != tag]
    if not pending:
        return
    log.info("Informes semanales: %s ligas pendientes", len(pending))
    sem = asyncio.Semaphore(REPORT_PARALLEL)

    async def one(league) -> None:
        async with sem:
            chat_id, thread_key = league["chat_id"], league["thread_key"]
            lang = lang_of(league)
            parts = [t(lang, "weekly_title")]
            try:
                if league["scoreboard_weekly"]:
                    current = await fetch_scoreboard(http(ctx), league["league_id"], league["sport"])
                    last_week = (current.week - 1) if current.week and current.week > 1 else None
                    parts.append(await scoreboard_text(ctx, league, last_week))
                if league["standings_weekly"]:
                    parts.append(await standings_text(ctx, league))
                await ctx.bot.send_message(chat_id, "\n\n".join(parts), message_thread_id=thread_key or None,
                                           link_preview_options=NO_PREVIEW)
            except Exception:
                log.exception("Error en el informe semanal de la liga %s", league["league_id"])
                return
            db.update_league(chat_id, thread_key, last_report=tag)
            await asyncio.sleep(1)  # margen para los límites de Telegram y Fleaflicker

    await asyncio.gather(*(one(league) for league in pending))


# ---------- Chequeo periódico ----------

async def check_league(ctx: ContextTypes.DEFAULT_TYPE, league) -> None:
    """Anuncia el nuevo OTC, recuerda el actual cada X horas y avisa al commish si se alarga."""
    chat_id, thread_key = league["chat_id"], league["thread_key"]
    db = storage(ctx)

    async def send(text: str) -> None:
        await ctx.bot.send_message(chat_id, text, message_thread_id=thread_key or None, link_preview_options=NO_PREVIEW)

    lang = lang_of(league)
    state = await load_state(ctx, league)
    users = db.users(chat_id, thread_key)
    pick, now = state.on_the_clock, time.time()

    if pick is None:
        await send(t(lang, "draft_finished"))
        db.update_league(chat_id, thread_key, drafting=0, last_overall=None)
        return

    if league["last_overall"] != pick.overall:
        await send(otc_line(state, users, lang))
        db.update_league(chat_id, thread_key, last_overall=pick.overall, otc_since=now,
                         last_reminder=now, commish_notified=0)
        return

    if quiet_now():
        return

    waited_h = active_seconds(league["otc_since"], now) / 3600
    if active_seconds(league["last_reminder"], now) >= league["reminder_hours"] * 3600:
        await send(t(lang, "still_otc", who=who(pick.team, users), hours=waited_h))
        db.update_league(chat_id, thread_key, last_reminder=now)
    if not league["commish_notified"] and league["commish"] and waited_h >= league["notify_hours"]:
        await send(t(lang, "commish_alert", commish=league["commish"], team=pick.team.name, hours=waited_h))
        db.update_league(chat_id, thread_key, commish_notified=1)


# Cuántas ligas se revisan a la vez: rápido con muchas ligas sin saturar a Fleaflicker.
PARALLEL_CHECKS = int(os.environ.get("PARALLEL_CHECKS", "8"))


async def check_all(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sem = asyncio.Semaphore(PARALLEL_CHECKS)

    async def one(league) -> None:
        async with sem:
            try:
                await check_league(ctx, league)
            except Exception:
                log.exception("Error revisando liga %s en chat %s", league["league_id"], league["chat_id"])

    await asyncio.gather(*(one(league) for league in storage(ctx).drafting_leagues()))


# ---------- Arranque ----------

COMMANDS = {
    "start": cmd_help, "help": cmd_help, "ping": cmd_ping,
    "setleague": cmd_setleague, "getchannelid": cmd_getchannelid, "getteams": cmd_getteams,
    "whosotc": cmd_whosotc, "getleague": cmd_getleague, "getleagueinfo": cmd_getleagueinfo,
    "getmissingusers": cmd_getmissingusers, "unsetleague": cmd_unsetleague, "setcommish": cmd_setcommish,
    "addtelegramusers": cmd_addtelegramusers, "startdraft": cmd_startdraft, "stopdraft": cmd_stopdraft,
    "getillegalrosters": cmd_getillegalrosters,
    "getleaguestanding": cmd_getleaguestanding, "getscoreboard": cmd_getscoreboard,
    "enablestandingnotification": cmd_enablestanding, "disablestandingnotification": cmd_disablestanding,
    "enablescoreboardnotification": cmd_enablescoreboard, "disablescoreboardnotification": cmd_disablescoreboard,
    "setreminderduration": cmd_setreminderduration, "setnotificationduration": cmd_setnotificationduration,
}


async def on_startup(app: Application) -> None:
    app.bot_data["http"] = httpx.AsyncClient(headers={"User-Agent": "CallerBot/1.0"})


async def on_shutdown(app: Application) -> None:
    await app.bot_data["http"].aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN (ver .env.example)")
    interval = int(os.environ.get("CHECK_INTERVAL", "300"))

    app = Application.builder().token(token).post_init(on_startup).post_shutdown(on_shutdown).build()
    # En Railway, si hay un volumen montado guardamos ahí la base de datos para que sobreviva a los reinicios.
    # Una ruta relativa en DB_PATH (como la de .env.example) también va dentro del volumen.
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    db_path = os.environ.get("DB_PATH") or "callerbot.db"
    if volume and not os.path.isabs(db_path):
        db_path = os.path.join(volume, db_path)
    app.bot_data["storage"] = Storage(db_path)
    log.info("Base de datos en %s (%s)", db_path, "volumen de Railway" if volume else "SIN volumen: se borra al reiniciar")

    # python-telegram-bot compara los comandos sin distinguir mayúsculas: /setLeague == /setleague
    for name, handler in COMMANDS.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(OTC_TEXT), cmd_whosotc))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(check_all, interval=interval, first=10)
    app.job_queue.run_repeating(weekly_reports, interval=900, first=60)
    log.info("CallerBot arrancado; revisando drafts cada %ss", interval)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
