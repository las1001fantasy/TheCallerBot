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
    """Inicializa la estructura de tablas y migra user_mappings si es necesario."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Comprobación y Migración Automática de la tabla antigua
    try:
        cursor.execute("ALTER TABLE user_mappings DROP COLUMN IF EXISTS chat_id CASCADE;")
        conn.commit()
    except Exception as e:
        logger.warning(f"Aviso de migración (se resolverá recreando la tabla): {e}")
        conn.rollback()
        cursor.execute("DROP TABLE IF EXISTS user_mappings CASCADE;")
        conn.commit()

    # 2. Creación de la estructura correcta
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
    """Obtiene el mapa {user_id: team_name} desde Fleaflicker."""
    url = f"https://www.fleaflicker.com/api/FetchLeagueRosters?sport=NFL&league_id={league_id}"
    teams_map = {}
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            for roster in data.get("rosters", []):
                team = roster.get("team", {})
                team_name = team.get("name", "Sin Nombre")
                owners = team.get("owners", [])
                for owner in owners:
                    owner_id = str(owner.get("id"))
                    teams_map[owner_id] = team_name
    except Exception as e:
        logger.error(f"Error al obtener equipos de Fleaflicker: {e}")
    return teams_map

# --- HANDLERS DE COMANDOS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /start con el menú de ayuda."""
    await update.message.reply_text(
        "¡Hola! El bot está iniciado y listo.\n\n"
        "**Comandos disponibles:**\n"
        "• `/vincular <id_fleaflicker>` - Vincular tu propia cuenta\n"
        "• `/vincular <id_fleaflicker> <@nick>` - Vincular a otro manager\n"
        "• `/setLeague <league_id> <commish>` - Configurar la liga de este chat\n"
        "• `/setAlerts <h_user> <h_commish>` - Configurar avisos\n"
        "• `/managers` - Ver los equipos y managers vinculados",
        parse_mode="Markdown"
    )

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configura la liga de Fleaflicker asociada a este chat."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setLeague <league_id> <commish_handle>` (ej: `/setLeague 123456 @comisionado`)", parse_mode="Markdown")
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
    """Mapea el ID de Fleaflicker con un nick de Telegram (propio o de un tercero)."""
    user = update.effective_user

    if len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ **Uso del comando /vincular:**\n\n"
            "• **Para ti:** `/vincular <id_fleaflicker>`\n"
            "• **Para otro:** `/vincular <id_fleaflicker> <@telegram_nick>`\n\n"
            "Ejemplo: `/vincular 123456 @JuanPerez`",
            parse_mode="Markdown"
        )
        return

    fleaflicker_id = context.args[0]

    # Determinar si vincula al usuario actual o a un tercero
    if len(context.args) >= 2:
        handle = context.args[1]
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
            f"✅ **Mapeo guardado exitosamente:**\n"
            f"• Fleaflicker ID: `{fleaflicker_id}`\n"
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
        
        # 1. Obtener la liga configurada para este chat
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()
        
        # 2. Obtener los mapeos globales de la BD
        cursor.execute("SELECT platform_user_id, telegram_handle FROM user_mappings;")
        mappings = dict(cursor.fetchall())  # { "id_fleaflicker": "@nick" }
        
        cursor.close()
        conn.close()

        if not league_row:
            await update.message.reply_text("⚠️ Primero debes configurar la liga en este chat usando `/setLeague <league_id> <@comisionado>`", parse_mode="Markdown")
            return

        league_id = league_row[0]

        # 3. Traer los equipos directamente de la API de Fleaflicker
        teams_map = get_fleaflicker_teams(league_id)

        if not teams_map:
            await update.message.reply_text("⚠️ No se pudieron obtener los equipos desde Fleaflicker. Revisa si el `league_id` es correcto.")
            return

        texto = "📋 **Estado de Managers en la Liga**\n\n"
        
        # 4. Construir la lista mostrando Equipo ➔ Telegram Nick
        for f_id, team_name in teams_map.items():
            handle = mappings.get(f_id)
            if handle:
                texto += f"🏈 **{team_name}**\n└ 👤 {handle} (`{f_id}`)\n\n"
            else:
                texto += f"🏈 **{team_name}**\n└ ❌ *Sin vincular* (`{f_id}`)\n\n"

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

    # Registro de Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("setAlerts", set_alerts))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    # Tarea repetitiva cada 90 segundos
    if app.job_queue:
        app.job_queue.run_repeating(check_otc_job, interval=90, first=10)

    logger.info("Bot iniciado correctamente...")
    app.run_polling()

if __name__ == "__main__":
    main()