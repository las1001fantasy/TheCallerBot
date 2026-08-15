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
    """Inicializa la estructura de tablas en PostgreSQL."""
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

        -- TABLA GLOBAL: Sin chat_id para compartir el mapeo entre múltiples ligas
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
        logger.error(f"Error al consultar Fleaflicker: {e}")
    return None

# --- HANDLERS DE COMANDOS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /start."""
    await update.message.reply_text(
        "¡Hola! El bot está iniciado y listo.\n\n"
        "Comandos disponibles:\n"
        "• `/vincular <id_fleaflicker>` - Mapea tu ID globalmente\n"
        "• `/setLeague <league_id> <commish_handle>` - Configura la liga de este chat\n"
        "• `/setAlerts <horas_usuario> <horas_commish>` - Ajusta los tiempos de las alertas\n"
        "• `/managers` - Muestra la lista de managers mapeados",
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
    """Mapea globalmente el ID de Fleaflicker con el nick de Telegram."""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Uso: `/vincular <fleaflicker_user_id>`", parse_mode="Markdown")
        return

    fleaflicker_id = context.args[0]
    handle = f"@{user.username}" if user.username else user.first_name

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_mappings (platform_user_id, telegram_handle, telegram_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (platform_user_id) 
        DO UPDATE SET telegram_handle = EXCLUDED.telegram_handle, telegram_id = EXCLUDED.telegram_id;
    """, (fleaflicker_id, handle, user.id))
    conn.commit()
    cursor.close()
    conn.close()

    await update.message.reply_text(f"✅ Mapeo global guardado: `{fleaflicker_id}` ➔ {handle}\nYa no requerirá vincularse en otras ligas.", parse_mode="Markdown")

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el listado de usuarios mapeados en la base de datos."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT platform_user_id, telegram_handle FROM user_mappings;")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        await update.message.reply_text("No hay ningún manager vinculado todavía.")
        return

    texto = "📋 **Managers Vinculados Globalmente:**\n\n"
    for f_id, handle in rows:
        texto += f"• Fleaflicker ID `{f_id}` ➔ {handle}\n"

    await update.message.reply_text(texto, parse_mode="Markdown")

# --- TAREA PROGRAMADA (JOB QUEUE) ---

async def check_otc_job(context: ContextTypes.DEFAULT_TYPE):
    """Tarea periódica que comprueba quién está en el turno (OTC) del Draft."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, league_id, commish_handle, user_alert_hours, commish_alert_hours FROM leagues;")
    active_leagues = cursor.fetchall()

    for chat_id, league_id, commish_handle, user_hours, commish_hours in active_leagues:
        data = get_fleaflicker_draft_status(league_id)
        if not data or "rows" not in data.get("minDraftBoard", {}):
            continue

        # Lógica de detección de pick actual en el Draft
        # Se verifica si hay un usuario OTC y se calculan los avisos
        pass

    cursor.close()
    conn.close()

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