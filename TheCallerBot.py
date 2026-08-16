import os
import logging
from datetime import datetime, timezone
import html
import requests
import psycopg
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

# Configuración de logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variables de Entorno
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Establece conexión con PostgreSQL."""
    return psycopg.connect(DATABASE_URL)

def init_db():
    """Inicializa la estructura de la base de datos."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leagues (
            chat_id BIGINT PRIMARY KEY,
            platform VARCHAR(20) NOT NULL,
            league_id VARCHAR(50) NOT NULL,
            commish_handle VARCHAR(50) NOT NULL,
            user_alert_hours INT DEFAULT 2,
            commish_alert_hours INT DEFAULT 8,
            draft_active BOOLEAN DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS user_mappings (
            fleaflicker_id VARCHAR(100) PRIMARY KEY,
            team_name VARCHAR(150),
            telegram_handle VARCHAR(50) NOT NULL,
            telegram_id BIGINT
        );

        CREATE TABLE IF NOT EXISTS draft_state (
            chat_id BIGINT PRIMARY KEY REFERENCES leagues(chat_id) ON DELETE CASCADE,
            current_pick_overall INT,
            otc_team_id VARCHAR(100),
            otc_start_time TIMESTAMP WITH TIME ZONE,
            user_alert_sent BOOLEAN DEFAULT FALSE,
            commish_alert_sent BOOLEAN DEFAULT FALSE
        );
    """)

    try:
        cursor.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS draft_active BOOLEAN DEFAULT FALSE;")
    except Exception:
        pass

    conn.commit()
    cursor.close()
    conn.close()

# --- AUXILIARES FLEAFLICKER ---

def parse_team_data(team_obj):
    team_id = str(team_obj.get("id")).strip() if team_obj.get("id") else None
    team_name = team_obj.get("name") or team_obj.get("nickname") or "Equipo sin nombre"
    
    user_id = None
    username = None
    identifiers = set()

    if team_id:
        identifiers.add(team_id)
    if team_name:
        identifiers.add(team_name.strip().lower())

    for owner in team_obj.get("owners", []):
        if owner.get("id"):
            u_id = str(owner.get("id")).strip()
            user_id = user_id or u_id
            identifiers.add(u_id)
        if owner.get("username"):
            u_name = str(owner.get("username")).strip()
            username = username or u_name
            identifiers.add(u_name.lower())

        user_obj = owner.get("user", {})
        if user_obj:
            if user_obj.get("id"):
                u_id = str(user_obj.get("id")).strip()
                user_id = user_id or u_id
                identifiers.add(u_id)
            if user_obj.get("username"):
                u_name = str(user_obj.get("username")).strip()
                username = username or u_name
                identifiers.add(u_name.lower())

    return {
        "team_id": team_id,
        "team_name": team_name,
        "user_id": user_id or "Sin ID",
        "username": username or "Sin Username",
        "identifiers": [i for i in identifiers if i]
    }

def get_fleaflicker_teams_info(league_id: str):
    teams_dict = {}
    current_year = datetime.now(timezone.utc).year

    urls = [
        f"https://www.fleaflicker.com/api/FetchLeagueRosters?sport=nfl&league_id={league_id}&season={current_year}",
        f"https://www.fleaflicker.com/api/FetchLeagueStandings?sport=nfl&league_id={league_id}&season={current_year}"
    ]

    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for roster in data.get("rosters", []):
                    parsed = parse_team_data(roster.get("team", {}))
                    if parsed["team_id"]:
                        teams_dict[parsed["team_id"]] = parsed
                for div in data.get("divisions", []):
                    for team in div.get("teams", []):
                        parsed = parse_team_data(team)
                        if parsed["team_id"]:
                            teams_dict[parsed["team_id"]] = parsed
        except Exception as e:
            logger.error(f"Error consultando Fleaflicker ({url}): {e}")

    return list(teams_dict.values())

def get_current_otc_data(league_id: str):
    """Obtiene el pick activo inspeccionando los picks sin jugador de Fleaflicker."""
    current_year = datetime.now(timezone.utc).year
    
    urls = [
        f"https://www.fleaflicker.com/api/FetchLeagueDraftBoard?sport=nfl&league_id={league_id}&season={current_year}",
        f"https://www.fleaflicker.com/api/FetchLeagueDraftPayload?sport=nfl&league_id={league_id}&season={current_year}"
    ]

    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                
                # 1. Buscar en la lista plana de picks (si existe)
                picks = data.get("picks", [])
                
                # Si no está en 'picks', buscar dentro de 'rows' -> 'cells'
                if not picks:
                    for row in data.get("rows", []):
                        for cell in row.get("cells", []):
                            picks.append(cell)

                # Buscar el PRIMER pick que NO tenga jugador asignado
                for pick in picks:
                    has_player = bool(pick.get("player") or pick.get("playerInfo"))
                    team = pick.get("team")

                    if not has_player and team:
                        team_info = parse_team_data(team)
                        
                        # Extraer número de pick/ronda/slot
                        pick_overall = pick.get("overall") or pick.get("pick") or 1
                        round_num = pick.get("round") or 1
                        slot_num = pick.get("slot") or pick.get("roundSlot") or 1

                        return {
                            "team_id": team_info["team_id"],
                            "team_name": team_info["team_name"],
                            "pick_overall": pick_overall,
                            "round": round_num,
                            "slot": slot_num,
                            "identifiers": team_info["identifiers"]
                        }
        except Exception as e:
            logger.error(f"Error consultando draft en url {url}: {e}")

    return None

def resolve_telegram_handle(team_identifiers, db_mappings):
    """Mapea los identificadores del equipo con la base de datos de Telegram."""
    for ident in team_identifiers:
        if ident.lower() in db_mappings:
            return db_mappings[ident.lower()]
    return None

# --- COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "📖 <b>Guía de Comandos del Bot</b>\n\n"
        "<b>1. Configuración de Liga</b>\n"
        "• <code>/setLeague &lt;league_id&gt; &lt;@comisionado&gt;</code>\n"
        "• <code>/vincular &lt;equipo_o_usuario&gt; &lt;@telegram_nick&gt;</code>\n"
        "• <code>/managers</code>\n\n"
        "<b>2. Control del Draft</b>\n"
        "• <code>/whosOTC</code> : Muestra quién está en turno de elegir.\n"
        "• <code>/startdraft</code> : Inicia el rastreo automático del draft.\n"
        "• <code>/stopdraft</code> : Detiene el rastreo del draft."
    )
    await update.message.reply_text(mensaje, parse_mode="HTML")

async def whos_otc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Indica quién está OTC en la liga vinculada al chat actual."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ No hay ninguna liga vinculada a este chat.")
            return

        league_id = league_row[0]

        cursor.execute("SELECT fleaflicker_id, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        db_map = {f_id.lower(): t_handle for f_id, t_handle in mappings}
        otc_info = get_current_otc_data(league_id)

        if not otc_info:
            await update.message.reply_text("🏈 <b>No hay ningún pick activo en este momento.</b> (El draft puede estar pausado o finalizado).", parse_mode="HTML")
            return

        handle = resolve_telegram_handle(otc_info["identifiers"], db_map)
        user_display = html.escape(handle) if handle else "<i>(Sin vincular)</i>"
        team_name = html.escape(otc_info["team_name"])

        mensaje = (
            f"🎯 <b>ON THE CLOCK (OTC)</b>\n\n"
            f"🏈 <b>Equipo:</b> {team_name}\n"
            f"👤 <b>Manager:</b> {user_display}\n"
            f"📍 <b>Pick:</b> Ronda {otc_info['round']}, Selección {otc_info['slot']} (#<code>{otc_info['pick_overall']}</code> overall)"
        )

        await update.message.reply_text(mensaje, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /whosOTC: {e}")
        await update.message.reply_text(f"❌ Error al consultar el draft: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa el seguimiento automático del draft para esta liga."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ Registra primero una liga con <code>/setLeague</code>.", parse_mode="HTML")
            return

        league_id = row[0]
        cursor.execute("UPDATE leagues SET draft_active = TRUE WHERE chat_id = %s;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()

        await update.message.reply_text(
            f"🚀 <b>¡Monitoreo de Draft Activado!</b>\n\n"
            f"La liga <code>{html.escape(league_id)}</code> está siendo rastreada. "
            f"El bot enviará notificaciones cuando le toque elegir a un nuevo manager.",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error en /startdraft: {e}")

async def stop_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Desactiva el seguimiento del draft."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE leagues SET draft_active = FALSE WHERE chat_id = %s;", (chat_id,))
        cursor.execute("DELETE FROM draft_state WHERE chat_id = %s;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()

        await update.message.reply_text("🛑 <b>Monitoreo de Draft detenido.</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error en /stopdraft: {e}")

# --- TAREA EN SEGUNDO PLANO (JOB QUEUE) ---

async def check_draft_updates(context: ContextTypes.DEFAULT_TYPE):
    """Tarea recurrente que revisa el estado del draft en las ligas activas."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT chat_id, league_id, user_alert_hours, commish_alert_hours, commish_handle FROM leagues WHERE draft_active = TRUE;")
        active_leagues = cursor.fetchall()

        if not active_leagues:
            cursor.close()
            conn.close()
            return

        cursor.execute("SELECT fleaflicker_id, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        db_map = {f_id.lower(): t_handle for f_id, t_handle in mappings}

        now = datetime.now(timezone.utc)

        for chat_id, league_id, user_h, commish_h, commish_handle in active_leagues:
            otc_data = get_current_otc_data(league_id)
            if not otc_data:
                continue

            cursor.execute("SELECT current_pick_overall, otc_team_id, otc_start_time, user_alert_sent, commish_alert_sent FROM draft_state WHERE chat_id = %s;", (chat_id,))
            state = cursor.fetchone()

            curr_pick = otc_data["pick_overall"]
            curr_team_id = otc_data["team_id"]
            handle = resolve_telegram_handle(otc_data["identifiers"], db_map)
            mention_user = handle if handle else html.escape(otc_data["team_name"])

            # 1. Si no hay estado previo o cambió el pick -> Anunciar Nuevo OTC
            if not state or state[0] != curr_pick:
                cursor.execute("""
                    INSERT INTO draft_state (chat_id, current_pick_overall, otc_team_id, otc_start_time, user_alert_sent, commish_alert_sent)
                    VALUES (%s, %s, %s, %s, FALSE, FALSE)
                    ON CONFLICT (chat_id) DO UPDATE SET
                        current_pick_overall = EXCLUDED.current_pick_overall,
                        otc_team_id = EXCLUDED.otc_team_id,
                        otc_start_time = EXCLUDED.otc_start_time,
                        user_alert_sent = FALSE,
                        commish_alert_sent = FALSE;
                """, (chat_id, curr_pick, curr_team_id, now))
                conn.commit()

                msg = (
                    f"📢 <b>¡NUEVO EN TURNO (OTC)!</b>\n\n"
                    f"👉 {mention_user} estás <b>On The Clock</b>.\n"
                    f"📍 Pick #{curr_pick} (Ronda {otc_data['round']}, Slot {otc_data['slot']})"
                )
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")

            # 2. Control de tiempo de alertas acumuladas
            else:
                _, _, start_time, user_sent, commish_sent = state
                if start_time:
                    elapsed_hours = (now - start_time).total_seconds() / 3600.0

                    # Alerta Usuario
                    if elapsed_hours >= user_h and not user_sent:
                        cursor.execute("UPDATE draft_state SET user_alert_sent = TRUE WHERE chat_id = %s;", (chat_id,))
                        conn.commit()
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏰ <b>RECORDATORIO OTC</b>\n\n{mention_user}, han pasado <b>{user_h} horas</b> y sigues en turno.",
                            parse_mode="HTML"
                        )

                    # Alerta Comisionado
                    if elapsed_hours >= commish_h and not commish_sent:
                        cursor.execute("UPDATE draft_state SET commish_alert_sent = TRUE WHERE chat_id = %s;", (chat_id,))
                        conn.commit()
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⚠️ <b>ALERTA COMISIONADO</b>\n\n{html.escape(commish_handle)}, {mention_user} ha superado el tiempo límite de <b>{commish_h} horas</b>.",
                            parse_mode="HTML"
                        )

        cursor.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error en job check_draft_updates: {e}")

# --- HANDLERS Y MAIN ---

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: <code>/setLeague &lt;league_id&gt; &lt;commish_handle&gt;</code>", parse_mode="HTML")
        return

    league_id, commish_handle = context.args[0], context.args[1]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leagues (chat_id, platform, league_id, commish_handle)
            VALUES (%s, 'fleaflicker', %s, %s)
            ON CONFLICT (chat_id) DO UPDATE 
            SET league_id = EXCLUDED.league_id, commish_handle = EXCLUDED.commish_handle;
        """, (chat_id, league_id, commish_handle))
        conn.commit()
        cursor.close()
        conn.close()

        await update.message.reply_text(f"✅ Liga <code>{html.escape(league_id)}</code> configurada.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error en /setLeague: {e}")

async def desvincular_liga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leagues WHERE chat_id = %s;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()
        await update.message.reply_text("🗑️ Liga desvinculada.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error en /desvincularLiga: {e}")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Uso: <code>/vincular &lt;equipo_o_usuario&gt; &lt;@nick&gt;</code>", parse_mode="HTML")
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ Configura primero una liga con <code>/setLeague</code>.", parse_mode="HTML")
            return

        league_id = row[0]

        if len(context.args) >= 2 and context.args[-1].startswith("@"):
            handle = context.args[-1].strip()
            fleaflicker_input = " ".join(context.args[:-1]).strip()
            telegram_id = None
        else:
            fleaflicker_input = " ".join(context.args).strip()
            handle = f"@{user.username}" if user.username else f"@{user.first_name}"
            telegram_id = user.id

        teams = get_fleaflicker_teams_info(league_id)
        target_team_id, detected_team_name = None, None
        clean_input = fleaflicker_input.lower().strip()

        for team in teams:
            t_name_clean = team["team_name"].lower().strip()
            if clean_input == t_name_clean or any(clean_input == ident.lower() for ident in team["identifiers"]):
                target_team_id = team["team_id"]
                detected_team_name = team["team_name"]
                break

        if not target_team_id:
            for team in teams:
                if clean_input in team["team_name"].lower().strip():
                    target_team_id = team["team_id"]
                    detected_team_name = team["team_name"]
                    break

        db_key = target_team_id if target_team_id else fleaflicker_input
        team_display = detected_team_name if detected_team_name else "Sin equipo asignado"

        cursor.execute("""
            INSERT INTO user_mappings (fleaflicker_id, team_name, telegram_handle, telegram_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fleaflicker_id) DO UPDATE SET 
                team_name = EXCLUDED.team_name,
                telegram_handle = EXCLUDED.telegram_handle,
                telegram_id = COALESCE(EXCLUDED.telegram_id, user_mappings.telegram_id);
        """, (db_key, team_display, handle, telegram_id))

        conn.commit()
        cursor.close()
        conn.close()

        msg = (
            f"✅ <b>Vinculación Registrada:</b>\n\n"
            f"• <b>Búsqueda:</b> {html.escape(fleaflicker_input)}\n"
            f"• <b>ID Equipo:</b> <code>{html.escape(str(db_key))}</code>\n"
            f"• <b>Equipo:</b> {html.escape(team_display)}\n"
            f"• <b>Telegram:</b> {html.escape(handle)}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /vincular: {e}")

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ Configura primero la liga con <code>/setLeague</code>.", parse_mode="HTML")
            return

        league_id = row[0]
        cursor.execute("SELECT fleaflicker_id, team_name, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        teams = get_fleaflicker_teams_info(league_id)
        db_map = {f_id.lower(): (t_name, t_handle) for f_id, t_name, t_handle in mappings}

        texto = f"📋 <b>Relación de Managers (Liga <code>{html.escape(str(league_id))}</code>)</b>\n\n"
        for team in teams:
            t_name, t_id = team["team_name"], team["team_id"]
            matched_handle = None

            if t_id and t_id.lower() in db_map:
                _, matched_handle = db_map[t_id.lower()]

            if not matched_handle:
                for ident in team["identifiers"]:
                    if ident.lower() in db_map:
                        _, matched_handle = db_map[ident.lower()]
                        break

            t_name_clean, t_id_clean = html.escape(t_name), html.escape(str(t_id))

            if matched_handle:
                texto += f"🏈 <b>{t_name_clean}</b>\n├ 🆔 Team ID: <code>{t_id_clean}</code>\n└ 👤 Telegram: {html.escape(matched_handle)}\n\n"
            else:
                texto += f"🏈 <b>{t_name_clean}</b>\n└ ❌ <i>Sin vincular</i>\n\n"

        await update.message.reply_text(texto, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /managers: {e}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("desvincularLiga", desvincular_liga))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))
    app.add_handler(CommandHandler("whosOTC", whos_otc))
    app.add_handler(CommandHandler("startdraft", start_draft))
    app.add_handler(CommandHandler("stopdraft", stop_draft))

    # Tarea en segundo plano (revisa el draft cada 60 segundos)
    if app.job_queue:
        app.job_queue.run_repeating(check_draft_updates, interval=60, first=10)

    logger.info("Bot iniciado con monitoreo de drafts...")
    app.run_polling()

if __name__ == "__main__":
    main()