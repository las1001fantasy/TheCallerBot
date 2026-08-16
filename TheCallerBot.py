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

# Configuración de Variables de Entorno
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Establece conexión con la base de datos PostgreSQL en Railway."""
    return psycopg.connect(DATABASE_URL)

def init_db():
    """Inicializa la estructura de tablas garantizando la persistencia de datos."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leagues (
            chat_id BIGINT PRIMARY KEY,
            platform VARCHAR(20) NOT NULL,
            league_id VARCHAR(50) NOT NULL,
            commish_handle VARCHAR(50) NOT NULL,
            user_alert_hours INT DEFAULT 2,
            commish_alert_hours INT DEFAULT 8
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
            otc_user_id VARCHAR(100),
            otc_start_time TIMESTAMP WITH TIME ZONE,
            user_alert_sent BOOLEAN DEFAULT FALSE,
            commish_alert_sent BOOLEAN DEFAULT FALSE
        );
    """)

    try:
        cursor.execute("ALTER TABLE user_mappings RENAME COLUMN platform_user_id TO fleaflicker_id;")
    except Exception:
        pass
    
    try:
        cursor.execute("ALTER TABLE user_mappings ADD COLUMN IF NOT EXISTS team_name VARCHAR(150);")
    except Exception:
        pass

    conn.commit()
    cursor.close()
    conn.close()

# --- FUNCIONES AUXILIARES DE FLEAFLICKER ---

def parse_team_data(team_obj):
    """Extrae datos de equipo e identificadores probando todas las variantes posibles."""
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
        if owner.get("displayName"):
            u_disp = str(owner.get("displayName")).strip()
            username = username or u_disp
            identifiers.add(u_disp.lower())

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
            if user_obj.get("displayName"):
                u_disp = str(user_obj.get("displayName")).strip()
                username = username or u_disp
                identifiers.add(u_disp.lower())

    return {
        "team_id": team_id,
        "team_name": team_name,
        "user_id": user_id or "Sin ID",
        "username": username or "Sin Username",
        "identifiers": [i for i in identifiers if i]
    }

def get_fleaflicker_teams_info(league_id: str):
    """Obtiene información de los equipos llamando a la API de Fleaflicker."""
    teams_dict = {}
    current_year = datetime.now(timezone.utc).year

    # 1. FetchLeagueRosters
    urls_rosters = [
        f"https://www.fleaflicker.com/api/FetchLeagueRosters?sport=nfl&league_id={league_id}&season={current_year}",
        f"https://www.fleaflicker.com/api/FetchLeagueRosters?sport=NFL&league_id={league_id}"
    ]
    for url in urls_rosters:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for roster in data.get("rosters", []):
                    team = roster.get("team", {})
                    parsed = parse_team_data(team)
                    if parsed["team_id"]:
                        teams_dict[parsed["team_id"]] = parsed
                if teams_dict and any(t["user_id"] != "Sin ID" for t in teams_dict.values()):
                    return list(teams_dict.values())
        except Exception as e:
            logger.error(f"Error en FetchLeagueRosters ({url}): {e}")

    # 2. FetchLeagueStandings
    urls_standings = [
        f"https://www.fleaflicker.com/api/FetchLeagueStandings?sport=nfl&league_id={league_id}&season={current_year}",
        f"https://www.fleaflicker.com/api/FetchLeagueStandings?sport=NFL&league_id={league_id}"
    ]
    for url in urls_standings:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for div in data.get("divisions", []):
                    for team in div.get("teams", []):
                        parsed = parse_team_data(team)
                        if parsed["team_id"]:
                            if parsed["team_id"] not in teams_dict:
                                teams_dict[parsed["team_id"]] = parsed
                            else:
                                teams_dict[parsed["team_id"]]["identifiers"] = list(
                                    set(teams_dict[parsed["team_id"]]["identifiers"] + parsed["identifiers"])
                                )
                                if teams_dict[parsed["team_id"]]["user_id"] == "Sin ID":
                                    teams_dict[parsed["team_id"]]["user_id"] = parsed["user_id"]
                                if teams_dict[parsed["team_id"]]["username"] == "Sin Username":
                                    teams_dict[parsed["team_id"]]["username"] = parsed["username"]
                if teams_dict:
                    return list(teams_dict.values())
        except Exception as e:
            logger.error(f"Error en FetchLeagueStandings ({url}): {e}")

    return list(teams_dict.values())

def resolve_fleaflicker_user_to_team(fleaflicker_input: str, league_id: str):
    """Resuelve nombre de usuario en Fleaflicker al team_id de la liga."""
    url_user = f"https://www.fleaflicker.com/api/FetchUserRosters?sport=nfl&username={fleaflicker_input}"
    try:
        res = requests.get(url_user, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for roster in data.get("rosters", []):
                league = roster.get("league", {})
                if str(league.get("id")) == str(league_id):
                    team = roster.get("team", {})
                    team_id = str(team.get("id")) if team.get("id") else None
                    team_name = team.get("name") or team.get("nickname")
                    return team_id, team_name
    except Exception as e:
        logger.error(f"Error resolviendo usuario Fleaflicker '{fleaflicker_input}': {e}")
    return None, None

# --- HANDLERS DE COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "📖 <b>Guía de Uso del Bot</b>\n\n"
        "<b>1. Configuración de Ligas</b>\n"
        "• <code>/setLeague &lt;league_id&gt; &lt;@comisionado&gt;</code>\n"
        "• <code>/desvincularLiga</code>\n"
        "• <code>/testLeague</code>\n"
        "• <code>/setAlerts &lt;horas_user&gt; &lt;horas_commish&gt;</code>\n\n"
        "<b>2. Gestión de Managers</b>\n"
        "• <code>/vincular &lt;nombre_equipo_o_usuario&gt;</code>\n"
        "• <code>/vincular &lt;nombre_equipo_o_usuario&gt; &lt;@telegram_nick&gt;</code>\n"
        "• <code>/managers</code>"
    )
    await update.message.reply_text(mensaje, parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! Bot activo. Usa <code>/help</code> para ver opciones.", parse_mode="HTML")

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: <code>/setLeague &lt;league_id&gt; &lt;commish_handle&gt;</code>", parse_mode="HTML")
        return

    league_id = context.args[0]
    commish_handle = context.args[1]

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
        await update.message.reply_text("❌ Error al guardar la liga.")

async def test_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ No hay liga asociada a este chat.")
            return

        league_id = league_row[0]

        cursor.execute("SELECT fleaflicker_id, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        db_map = {f_id.lower(): t_handle for f_id, t_handle in mappings}
        teams = get_fleaflicker_teams_info(league_id)

        if not teams:
            await update.message.reply_text(f"❌ No se obtuvieron datos de la Liga <code>{html.escape(league_id)}</code>.")
            return

        out = f"🔍 <b>Equipos e Identificadores en Liga <code>{html.escape(league_id)}</code>:</b>\n\n"
        for t in teams:
            t_id = t['team_id']
            u_id = t['user_id']
            u_name = t['username']
            matched_handle = None

            if t_id and t_id.lower() in db_map:
                matched_handle = db_map[t_id.lower()]
            else:
                for ident in t['identifiers']:
                    if ident.lower() in db_map:
                        matched_handle = db_map[ident.lower()]
                        break

            telegram_display = matched_handle if matched_handle else "Sin vincular"
            out += f"🏈 <b>{html.escape(t['team_name'])}</b> (Team ID: <code>{html.escape(str(t_id))}</code>)\n"
            out += f"IDs/Users: <code>{html.escape(str(u_id))} - {html.escape(str(u_name))} - {html.escape(str(telegram_display))}</code>\n\n"

        await update.message.reply_text(out, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /testLeague: {e}")
        await update.message.reply_text(f"❌ Error al consultar Fleaflicker: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

async def desvincular_liga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leagues WHERE chat_id = %s;", (chat_id,))
        rows = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if rows > 0:
            await update.message.reply_text("🗑️ <b>Liga desvinculada.</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ No hay liga configurada.")
    except Exception as e:
        logger.error(f"Error en /desvincularLiga: {e}")

async def set_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: <code>/setAlerts &lt;horas_user&gt; &lt;horas_commish&gt;</code>", parse_mode="HTML")
        return

    try:
        user_hours, commish_hours = int(context.args[0]), int(context.args[1])
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE leagues 
            SET user_alert_hours = %s, commish_alert_hours = %s
            WHERE chat_id = %s;
        """, (user_hours, commish_hours, chat_id))
        conn.commit()
        cursor.close()
        conn.close()
        await update.message.reply_text(f"⏰ Alertas actualizadas: User {user_hours}h, Commish {commish_hours}h.")
    except Exception as e:
        logger.error(f"Error en /setAlerts: {e}")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vinculación segura usando escape HTML para evitar errores de parseo."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Uso: <code>/vincular &lt;nombre_equipo_o_usuario&gt;</code> o <code>/vincular &lt;nombre_equipo_o_usuario&gt; &lt;@telegram_nick&gt;</code>", parse_mode="HTML")
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ <b>No hay liga vinculada a este chat.</b> Registra una con <code>/setLeague</code>.", parse_mode="HTML")
            return

        league_id = league_row[0]

        if len(context.args) >= 2 and context.args[-1].startswith("@"):
            handle = context.args[-1].strip()
            fleaflicker_input = " ".join(context.args[:-1]).strip()
            telegram_id = None
        else:
            fleaflicker_input = " ".join(context.args).strip()
            handle = f"@{user.username}" if user.username else f"@{user.first_name}"
            telegram_id = user.id

        teams = get_fleaflicker_teams_info(league_id)
        target_team_id = None
        detected_team_name = None

        clean_input = fleaflicker_input.lower().strip()

        # Match exacto o por identificadores
        for team in teams:
            t_name_clean = team["team_name"].lower().strip()
            if clean_input == t_name_clean or any(clean_input == ident.lower() for ident in team["identifiers"]):
                target_team_id = team["team_id"]
                detected_team_name = team["team_name"]
                break

        # Match flexible por coincidencia parcial de texto
        if not target_team_id:
            for team in teams:
                if clean_input in team["team_name"].lower().strip():
                    target_team_id = team["team_id"]
                    detected_team_name = team["team_name"]
                    break

        # Match vía API de Usuario
        if not target_team_id:
            res_team_id, res_team_name = resolve_fleaflicker_user_to_team(fleaflicker_input, league_id)
            if res_team_id:
                target_team_id = res_team_id
                detected_team_name = res_team_name

        if target_team_id:
            db_key = target_team_id
            team_display = detected_team_name
        else:
            db_key = fleaflicker_input
            team_display = "Sin equipo asignado (Solo Manager/Commish)"

        cursor.execute("""
            INSERT INTO user_mappings (fleaflicker_id, team_name, telegram_handle, telegram_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fleaflicker_id) 
            DO UPDATE SET 
                team_name = EXCLUDED.team_name,
                telegram_handle = EXCLUDED.telegram_handle, 
                telegram_id = COALESCE(EXCLUDED.telegram_id, user_mappings.telegram_id);
        """, (db_key, team_display, handle, telegram_id))

        conn.commit()
        cursor.close()
        conn.close()

        res_msg = (
            f"✅ <b>Vinculación Registrada:</b>\n\n"
            f"• <b>Búsqueda:</b> {html.escape(fleaflicker_input)}\n"
            f"• <b>ID Equipo:</b> <code>{html.escape(str(db_key))}</code>\n"
            f"• <b>Equipo:</b> {html.escape(team_display)}\n"
            f"• <b>Telegram:</b> {html.escape(handle)}"
        )

        await update.message.reply_text(res_msg, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /vincular: {e}")
        await update.message.reply_text(f"❌ Error al realizar la vinculación: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de managers mapeados de forma segura con parse_mode='HTML'."""
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ Configura primero la liga con <code>/setLeague</code>.", parse_mode="HTML")
            return

        league_id = league_row[0]
        cursor.execute("SELECT fleaflicker_id, team_name, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        teams = get_fleaflicker_teams_info(league_id)
        db_map = {f_id.lower(): (t_name, t_handle) for f_id, t_name, t_handle in mappings}

        texto = f"📋 <b>Relación de Managers (Liga <code>{html.escape(str(league_id))}</code>)</b>\n\n"
        for team in teams:
            t_name = team["team_name"]
            t_id = team["team_id"]
            matched_handle = None

            if t_id and t_id.lower() in db_map:
                _, matched_handle = db_map[t_id.lower()]

            if not matched_handle:
                for ident in team["identifiers"]:
                    if ident.lower() in db_map:
                        _, matched_handle = db_map[ident.lower()]
                        break

            t_name_clean = html.escape(t_name)
            t_id_clean = html.escape(str(t_id))

            if matched_handle:
                matched_clean = html.escape(matched_handle)
                texto += f"🏈 <b>{t_name_clean}</b>\n├ 🆔 Team ID: <code>{t_id_clean}</code>\n└ 👤 Telegram: {matched_clean}\n\n"
            else:
                texto += f"🏈 <b>{t_name_clean}</b>\n└ ❌ <i>Sin vincular</i>\n\n"

        await update.message.reply_text(texto, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error en /managers: {e}")
        await update.message.reply_text(f"❌ Error al obtener la lista de managers: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

# --- MAIN ---

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("desvincularLiga", desvincular_liga))
    app.add_handler(CommandHandler("testLeague", test_league))
    app.add_handler(CommandHandler("setAlerts", set_alerts))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    logger.info("Bot iniciado correctamente...")
    app.run_polling()

if __name__ == "__main__":
    main()