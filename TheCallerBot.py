import os
import logging
from datetime import datetime, timezone
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
            platform_user_id VARCHAR(100) PRIMARY KEY,
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
    conn.commit()
    cursor.close()
    conn.close()

# --- FUNCIONES AUXILIARES DE FLEAFLICKER ---

def get_fleaflicker_draft_status(league_id: str):
    """Consulta la API de Fleaflicker para obtener el estado del borrador."""
    url = f"https://www.fleaflicker.com/api/FetchLeagueDraftBoard?sport=NFL&league_id={league_id}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"Error al consultar Fleaflicker Draft Board: {e}")
    return None

def get_fleaflicker_teams(league_id: str):
    """Obtiene una lista de tuplas (team_name, set_of_identifiers) desde Fleaflicker."""
    teams = []
    
    url_rosters = f"https://www.fleaflicker.com/api/FetchLeagueRosters?sport=NFL&league_id={league_id}"
    try:
        res = requests.get(url_rosters, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for roster in data.get("rosters", []):
                team = roster.get("team", {})
                team_name = team.get("name") or team.get("nickname") or "Equipo sin nombre"
                identifiers = set()
                
                if team.get("id"):
                    identifiers.add(str(team.get("id")))
                
                for owner in team.get("owners", []):
                    if owner.get("id"):
                        identifiers.add(str(owner.get("id")))
                    if owner.get("displayName"):
                        identifiers.add(str(owner.get("displayName")).lower())
                    if owner.get("username"):
                        identifiers.add(str(owner.get("username")).lower())
                
                if identifiers:
                    teams.append((team_name, identifiers))
    except Exception as e:
        logger.error(f"Error en FetchLeagueRosters: {e}")

    if teams:
        return teams

    # Fallback a DraftBoard
    data_draft = get_fleaflicker_draft_status(league_id)
    if data_draft:
        rows = data_draft.get("minDraftBoard", {}).get("rows", [])
        teams_dict = {}
        for row in rows:
            for cell in row.get("cells", []):
                team = cell.get("team", {})
                t_id = team.get("id")
                t_name = team.get("name") or team.get("nickname")
                if t_id and t_name and t_id not in teams_dict:
                    identifiers = {str(t_id)}
                    for owner in team.get("owners", []):
                        if owner.get("id"):
                            identifiers.add(str(owner.get("id")))
                        if owner.get("displayName"):
                            identifiers.add(str(owner.get("displayName")).lower())
                    teams_dict[t_id] = (t_name, identifiers)
        teams = list(teams_dict.values())

    return teams

# --- HANDLERS DE COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la guía completa de instrucciones y comandos del bot."""
    mensaje = (
        "📖 **Guía de Uso del Bot**\n\n"
        "**1. Configuración de Ligas**\n"
        "• `/setLeague <league_id> <@comisionado>`\n"
        "  Asocia una liga de Fleaflicker a este chat.\n"
        "  _Ejemplo:_ `/setLeague 346999 @bocamolls`\n\n"
        "• `/desvincularLiga`\n"
        "  Elimina la liga configurada en este chat si te has equivocado.\n\n"
        "• `/setAlerts <horas_user> <horas_commish>`\n"
        "  Ajusta el tiempo de espera antes de notificar al manager en turno o al comisionado.\n"
        "  _Ejemplo:_ `/setAlerts 2 8`\n\n"
        "**2. Gestión de Managers**\n"
        "• `/vincular <id_o_user_fleaflicker>`\n"
        "  Vincula tu propio ID o Username de Fleaflicker con tu Telegram.\n"
        "  _Ejemplo:_ `/vincular Las1001`\n\n"
        "• `/vincular <id_o_user_fleaflicker> <@telegram_nick>`\n"
        "  (Comisionado/Admin) Vincula o reasigna a otro manager manualmente.\n"
        "  _Ejemplo:_ `/vincular Las1001 @daovir`\n\n"
        "• `/managers`\n"
        "  Muestra la lista de equipos de la liga actual y sus usuarios asignados.\n\n"
        "💡 *Nota:* La vinculación de managers es global. Si un manager se vincula una vez, el bot lo reconocerá automáticamente en cualquier otra liga donde juegue."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /start con una bienvenida e indicación de ayuda."""
    await update.message.reply_text(
        "¡Hola! El bot está iniciado y listo.\n\n"
        "Usa `/help` para ver la lista completa de comandos e instrucciones de configuración.",
        parse_mode="Markdown"
    )

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configura la liga de Fleaflicker asociada a este chat."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setLeague <league_id> <commish_handle>` (ej: `/setLeague 346999 @comisionado`)", parse_mode="Markdown")
        return

    league_id = context.args[0]
    commish_handle = context.args[1]

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

    await update.message.reply_text(f"✅ Liga `{league_id}` configurada para este chat. Comisionado: {commish_handle}", parse_mode="Markdown")

async def desvincular_liga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina la vinculación de la liga en el chat actual."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leagues WHERE chat_id = %s;", (chat_id,))
        rows_deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if rows_deleted > 0:
            await update.message.reply_text("🗑️ **Liga desvinculada.** Puedes volver a configurar una nueva con `/setLeague`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ No hay ninguna liga vinculada actualmente a este chat.")
    except Exception as e:
        logger.error(f"Error en /desvincularLiga: {e}")
        await update.message.reply_text("❌ Hubo un error al desvincular la liga.")

async def set_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ajusta las horas de aviso para el manager en turno y el comisionado."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setAlerts <horas_user> <horas_commish>` (ej: `/setAlerts 2 8`)", parse_mode="Markdown")
        return

    try:
        user_hours = int(context.args[0])
        commish_hours = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Por favor, ingresa números válidos para las horas.")
        return

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

    await update.message.reply_text(f"⏰ Alertas actualizadas: Aviso a usuario a las {user_hours}h, aviso a comisionado a las {commish_hours}h.")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mapea o reasigna el ID de Fleaflicker con un usuario de Telegram."""
    user = update.effective_user

    if len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ **Uso del comando /vincular:**\n\n"
            "• **Para ti:** `/vincular <id_o_user_fleaflicker>`\n"
            "• **Para otro:** `/vincular <id_o_user_fleaflicker> <@telegram_nick>`\n\n"
            "Ejemplo: `/vincular Las1001 @daovir`",
            parse_mode="Markdown"
        )
        return

    fleaflicker_id = context.args[0].strip()

    if len(context.args) >= 2:
        handle = context.args[1].strip()
        if not handle.startswith("@"):
            handle = f"@{handle}"
        telegram_id = None
    else:
        handle = f"@{user.username}" if user.username else f"@{user.first_name}"
        telegram_id = user.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO user_mappings (platform_user_id, telegram_handle, telegram_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (platform_user_id) 
            DO UPDATE SET telegram_handle = EXCLUDED.telegram_handle, 
                          telegram_id = COALESCE(EXCLUDED.telegram_id, user_mappings.telegram_id);
        """, (fleaflicker_id, handle, telegram_id))
        
        conn.commit()
        cursor.close()
        conn.close()

        await update.message.reply_text(
            f"✅ **Mapeo guardado/actualizado:**\n"
            f"• Identificador Fleaflicker: `{fleaflicker_id}`\n"
            f"• Telegram: {handle}", 
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error en /vincular: {e}")
        await update.message.reply_text("❌ Hubo un error al guardar la vinculación en la base de datos.")

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el listado de los equipos de la liga actual cruzados con los managers vinculados."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()
        
        cursor.execute("SELECT platform_user_id, telegram_handle FROM user_mappings;")
        mappings_raw = cursor.fetchall()
        
        cursor.close()
        conn.close()

        if not league_row:
            await update.message.reply_text("⚠️ Primero debes configurar la liga en este chat usando `/setLeague <league_id> <@comisionado>`", parse_mode="Markdown")
            return

        league_id = league_row[0]
        mappings = {str(k).lower(): v for k, v in mappings_raw}
        teams = get_fleaflicker_teams(league_id)

        if not teams:
            await update.message.reply_text(
                f"⚠️ **No se pudieron obtener los equipos desde Fleaflicker.**\n\n"
                f"• Revisa que el ID de liga sea puramente numérico (ej: `346999`).\n"
                f"• Si te equivocaste, desvincula esta liga con `/desvincularLiga` y configúrala de nuevo con `/setLeague`.",
                parse_mode="Markdown"
            )
            return

        texto = f"📋 **Estado de Managers en la Liga (`{league_id}`)**\n\n"
        
        for team_name, identifiers in teams:
            matched_handle = None
            matched_id = None

            for ident in identifiers:
                ident_lower = ident.lower()
                if ident_lower in mappings:
                    matched_handle = mappings[ident_lower]
                    matched_id = ident
                    break

            if matched_handle:
                texto += f"🏈 **{team_name}**\n└ 👤 {matched_handle} (`{matched_id}`)\n\n"
            else:
                sample_id = next(iter(identifiers)) if identifiers else "N/A"
                texto += f"🏈 **{team_name}**\n└ ❌ *Sin vincular* (ID: `{sample_id}`)\n\n"

        await update.message.reply_text(texto, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error en /managers: {e}")
        await update.message.reply_text("❌ Hubo un error al obtener la lista de managers.")

# --- TAREA PROGRAMADA (JOB QUEUE) ---

async def check_otc_job(context: ContextTypes.DEFAULT_TYPE):
    """Tarea periódica que comprueba el borrador de Fleaflicker."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, league_id, commish_handle, user_alert_hours, commish_alert_hours FROM leagues;")
        active_leagues = cursor.fetchall()

        for chat_id, league_id, commish_handle, user_hours, commish_hours in active_leagues:
            data = get_fleaflicker_draft_status(league_id)
            if not data or "rows" not in data.get("minDraftBoard", {}):
                continue

            # Lógica de revisión OTC
            pass

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error en check_otc_job: {e}")

# --- MAIN ---

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Registro de Handlers de Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("desvincularLiga", desvincular_liga))
    app.add_handler(CommandHandler("setAlerts", set_alerts))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    if app.job_queue:
        app.job_queue.run_repeating(check_otc_job, interval=90, first=10)

    logger.info("Bot iniciado correctamente...")
    app.run_polling()

if __name__ == "__main__":
    main()