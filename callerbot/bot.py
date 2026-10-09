"""CallerBot: anuncia en Telegram quién está on the clock en los drafts de Fleaflicker.

Cada canal (un grupo, o un tema dentro de un grupo con temas) tiene como mucho una liga.
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx
from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .fleaflicker import SPORTS, DraftState, FleaflickerError, Team, detect_sport, fetch_state
from .storage import Storage

log = logging.getLogger("callerbot")

HELP = """CallerBot avisa de quién está on the clock en los drafts de Fleaflicker.

Comandos públicos:
/ping - Comprueba que el bot está funcionando
/setLeague leagueID @commish (opcional) - Asigna la liga de Fleaflicker a este canal. El leagueID es el último número de la URL de la liga, p. ej. www.fleaflicker.com/nfl/leagues/312835
/getChannelID - Devuelve el ID del canal
/getTeams - Lista los equipos con su usuario de Fleaflicker y de Telegram
/whosOTC - Quién está on the clock (también vale escribir "who's on the clock")

Gestión de la liga (commish o admins del grupo):
/getLeague - Liga asignada a este canal
/getLeagueInfo - Información de la liga y del draft
/getMissingUsers - Usuarios de Fleaflicker sin usuario de Telegram
/unsetLeague - Quita la liga del canal
/setCommish @commish - Cambia el commish
/addTelegramUsers ffUser.@telegram ffUser2.@telegram2 ... - Asocia usuarios de Fleaflicker con Telegram (sin espacios dentro de los nombres)
/startDraft - Empieza a avisar de quién está OTC
/stopDraft - Deja de avisar
/setReminderDuration horas - Cada cuántas horas se recuerda quién está OTC (1-23, por defecto 2)
/setNotificationDuration horas - A las cuántas horas de OTC se avisa al commish (1-23, por defecto 8)"""

OTC_TEXT = re.compile(r"who'?s on the clock|qui[eé]n est[aá] on the clock", re.IGNORECASE)


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


async def reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, do_quote=False)


async def is_manager(update: Update, league) -> bool:
    """El commish de la liga, un admin del grupo, o cualquiera en un chat privado."""
    chat, user = update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return True
    if league and league["commish"] and user.username and norm(league["commish"]) == norm(f"@{user.username}"):
        return True
    member = await chat.get_member(user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def require_league(update: Update, ctx: ContextTypes.DEFAULT_TYPE, manager: bool):
    league = storage(ctx).get_league(*channel(update))
    if not league:
        await reply(update, "No hay liga en este canal. Usa /setLeague primero.")
        return None
    if manager and not await is_manager(update, league):
        await reply(update, "Solo el commish o un admin del grupo pueden usar este comando.")
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


def otc_line(state: DraftState, users: dict[str, str]) -> str:
    pick = state.on_the_clock
    if pick is None:
        return "El draft ha terminado."
    return f"{who(pick.team, users)} OTC\nRonda {pick.round}, pick {pick.slot} (#{pick.overall})"


def hours_arg(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    if len(ctx.args) == 1 and ctx.args[0].isdigit() and 1 <= int(ctx.args[0]) <= 23:
        return int(ctx.args[0])
    return None


# ---------- Comandos públicos ----------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "pong, estoy funcionando.")


async def cmd_getchannelid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, thread = channel(update)
    await reply(update, f"Channel ID: {chat_id}" + (f" · Tema: {thread}" if thread else ""))


async def cmd_setleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    if not args or not args[0].isdigit():
        await reply(update, "Uso: /setLeague leagueID @commish (opcional)")
        return
    current = storage(ctx).get_league(*channel(update))
    if current and not await is_manager(update, current):
        await reply(update, "Este canal ya tiene liga; solo el commish o un admin pueden cambiarla.")
        return

    league_id = int(args[0])
    commish = mention(args[1]) if len(args) > 1 and args[1].upper() not in SPORTS else None
    sport = next((a.upper() for a in args[1:] if a.upper() in SPORTS), None)
    try:
        sport = sport or await detect_sport(http(ctx), league_id)
        state = await fetch_state(http(ctx), league_id, sport)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, f"No he podido leer la liga: {e}")
        return

    storage(ctx).set_league(*channel(update), league_id, sport, commish)
    await reply(
        update,
        f"Liga {league_id} ({sport}) asignada a este canal"
        + (f", commish {commish}" if commish else "")
        + f". {len(state.teams)} equipos.\n"
        "Asocia los usuarios con /addTelegramUsers y empieza con /startDraft.",
    )


async def cmd_getteams(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, f"Error leyendo Fleaflicker: {e}")
        return
    users = storage(ctx).users(*channel(update))
    lines = [f"Equipos de la liga {league['league_id']} (Fleaflicker):"]
    for i, t in enumerate(state.teams, 1):
        owners = ", ".join(t.owners) or "sin dueño"
        tg = ", ".join(telegram_of(t, users)) or "sin Telegram"
        lines.append(f"{t.slot or i}. {t.name} · {owners} · {tg}")
    await reply(update, "\n".join(lines))


async def cmd_whosotc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = storage(ctx).get_league(*channel(update))
    if not league:
        if update.effective_message.text.startswith("/"):
            await reply(update, "No hay liga en este canal. Usa /setLeague primero.")
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, f"Error leyendo Fleaflicker: {e}")
        return
    await reply(update, otc_line(state, storage(ctx).users(*channel(update))))


# ---------- Gestión de la liga ----------

async def cmd_getleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if league:
        await reply(update, f"Liga {league['league_id']} ({league['sport']})")


async def cmd_getleagueinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, f"Error leyendo Fleaflicker: {e}")
        return
    users = storage(ctx).users(*channel(update))
    otc = f"{who(state.on_the_clock.team, users)} (#{state.on_the_clock.overall})" if state.on_the_clock else "draft terminado"
    await reply(
        update,
        f"Liga {league['league_id']} ({league['sport']})\n"
        f"Commish: {league['commish'] or 'sin asignar'}\n"
        f"Equipos: {len(state.teams)} · Picks hechos: {state.picks_made}\n"
        f"OTC: {otc}\n"
        f"Avisos: {'activados' if league['drafting'] else 'parados'} · "
        f"recordatorio cada {league['reminder_hours']} h · commish a las {league['notify_hours']} h",
    )


async def cmd_getmissingusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=False)
    if not league:
        return
    try:
        state = await load_state(ctx, league)
    except (FleaflickerError, httpx.HTTPError) as e:
        await reply(update, f"Error leyendo Fleaflicker: {e}")
        return
    users = storage(ctx).users(*channel(update))
    missing = [f"{o} ({t.name})" for t in state.teams for o in t.owners if norm(o) not in users]
    orphan = [t.name for t in state.teams if not t.owners and norm(t.name) not in users]
    if not missing and not orphan:
        await reply(update, "Todos los usuarios de Fleaflicker tienen Telegram asociado.")
        return
    lines = ["Sin usuario de Telegram:", *missing] if missing else []
    if orphan:
        lines += ["Equipos sin dueño en Fleaflicker (asócialos por nombre, sin espacios: NombreEquipo.@telegram):", *orphan]
    await reply(update, "\n".join(lines))


async def cmd_unsetleague(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if league:
        storage(ctx).unset_league(*channel(update))
        await reply(update, f"Liga {league['league_id']} quitada de este canal.")


async def cmd_setcommish(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    if len(ctx.args) != 1:
        await reply(update, "Uso: /setCommish @commish")
        return
    commish = mention(ctx.args[0])
    storage(ctx).update_league(*channel(update), commish=commish)
    await reply(update, f"Commish: {commish}")


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
        await reply(update, "Uso: /addTelegramUsers ffUser.@telegram ffUser2.@telegram2 ...")
        return
    lines = (["Añadidos:", *added] if added else []) + (["No entiendo:", *bad] if bad else [])
    await reply(update, "\n".join(lines))


async def cmd_startdraft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    storage(ctx).update_league(*channel(update), drafting=1, last_overall=None, commish_notified=0)
    await reply(update, "Draft en marcha. Avisaré en este canal cada vez que cambie el OTC.")
    await check_league(ctx, storage(ctx).get_league(*channel(update)))


async def cmd_stopdraft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if league:
        storage(ctx).update_league(*channel(update), drafting=0)
        await reply(update, "Avisos de OTC parados. /startDraft para reanudarlos.")


async def cmd_setreminderduration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    hours = hours_arg(ctx)
    if hours is None:
        await reply(update, "Uso: /setReminderDuration horas (1-23)")
        return
    storage(ctx).update_league(*channel(update), reminder_hours=hours)
    await reply(update, f"Recordaré quién está OTC cada {hours} h.")


async def cmd_setnotificationduration(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    league = await require_league(update, ctx, manager=True)
    if not league:
        return
    hours = hours_arg(ctx)
    if hours is None:
        await reply(update, "Uso: /setNotificationDuration horas (1-23)")
        return
    storage(ctx).update_league(*channel(update), notify_hours=hours)
    await reply(update, f"Avisaré al commish cuando alguien lleve {hours} h OTC.")


# ---------- Chequeo periódico ----------

async def check_league(ctx: ContextTypes.DEFAULT_TYPE, league) -> None:
    """Anuncia el nuevo OTC, recuerda el actual cada X horas y avisa al commish si se alarga."""
    chat_id, thread_key = league["chat_id"], league["thread_key"]
    db = storage(ctx)

    async def send(text: str) -> None:
        await ctx.bot.send_message(chat_id, text, message_thread_id=thread_key or None)

    state = await load_state(ctx, league)
    users = db.users(chat_id, thread_key)
    pick, now = state.on_the_clock, time.time()

    if pick is None:
        await send("El draft ha terminado. ¡Suerte a todos!")
        db.update_league(chat_id, thread_key, drafting=0, last_overall=None)
        return

    if league["last_overall"] != pick.overall:
        await send(otc_line(state, users))
        db.update_league(chat_id, thread_key, last_overall=pick.overall, otc_since=now,
                         last_reminder=now, commish_notified=0)
        return

    waited_h = (now - league["otc_since"]) / 3600
    if now - league["last_reminder"] >= league["reminder_hours"] * 3600:
        await send(f"{who(pick.team, users)} sigue OTC (desde hace {waited_h:.0f} h)")
        db.update_league(chat_id, thread_key, last_reminder=now)
    if not league["commish_notified"] and league["commish"] and waited_h >= league["notify_hours"]:
        await send(f"{league['commish']}: {pick.team.name} lleva {waited_h:.0f} h OTC.")
        db.update_league(chat_id, thread_key, commish_notified=1)


async def check_all(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for league in storage(ctx).drafting_leagues():
        try:
            await check_league(ctx, league)
        except Exception:
            log.exception("Error revisando liga %s en chat %s", league["league_id"], league["chat_id"])


# ---------- Arranque ----------

COMMANDS = {
    "start": cmd_help, "help": cmd_help, "ping": cmd_ping,
    "setleague": cmd_setleague, "getchannelid": cmd_getchannelid, "getteams": cmd_getteams,
    "whosotc": cmd_whosotc, "getleague": cmd_getleague, "getleagueinfo": cmd_getleagueinfo,
    "getmissingusers": cmd_getmissingusers, "unsetleague": cmd_unsetleague, "setcommish": cmd_setcommish,
    "addtelegramusers": cmd_addtelegramusers, "startdraft": cmd_startdraft, "stopdraft": cmd_stopdraft,
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
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    default_db = os.path.join(volume, "callerbot.db") if volume else "callerbot.db"
    app.bot_data["storage"] = Storage(os.environ.get("DB_PATH", default_db))

    # python-telegram-bot compara los comandos sin distinguir mayúsculas: /setLeague == /setleague
    for name, handler in COMMANDS.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(OTC_TEXT), cmd_whosotc))

    app.job_queue.run_repeating(check_all, interval=interval, first=10)
    log.info("CallerBot arrancado; revisando drafts cada %ss", interval)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
