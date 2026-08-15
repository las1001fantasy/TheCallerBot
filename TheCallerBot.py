import os
import sys
import logging
import requests
import psycopg2
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

# Configuración de Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración desde Variables de Entorno (Railway)
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DATABASE_URL:
    logger.error("Faltan las variables BOT_TOKEN o DATABASE_URL en el entorno.")
    sys.exit(1)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Inicializa la estructura de tablas si no existen."""
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
            chat_id BIGINT REFERENCES leagues(chat_id) ON DELETE CASCADE,
            platform_user_id VARCHAR(100) NOT NULL,
            telegram_handle VARCHAR(50) NOT NULL,
            PRIMARY KEY (chat_id, platform_user_id)
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

# --- CONSULTAS DE APIS ---

def get_fleaflicker_otc(league_id):
    """Consulta el estado del draft en Fleaflicker."""
    url = f"https://www.fleaflicker.com/api/FetchLeagueDraftBoard?sport=NFL&league_id={league_id}"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json()
        rows = data.get("rows", [])
        for row in rows:
            for item in row.get("cells", []):
                # Identificar el pick que está activo (OTC)
                if item.get("status") == "DRAFT_STATUS_IN_PROGRESS" or (item.get("isCurrent") and not item.get("player")):
                    team = item.get("team", {})
                    user = team.get("owners", [{}])[0]
                    user_id = str(user.get("id", ""))
                    # El tiempo de inicio se calcula del pick o marca actual
                    return {
                        "pick_overall": item.get("overall"),
                        "user_id": user_id,
                        "user_name": user.get("displayName", "Manager"),
                        "team_name": team.get("name", "Equipo")
                    }
    return None

# --- COMANDOS DE TELEGRAM ---

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sintaxis: /setLeague FLEAFLICKER <LEAGUE_ID> <@commish>"""
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("⚠️ Uso: `/setLeague FLEAFLICKER <LEAGUE_ID> <@commish>`", parse_mode="Markdown")
        return

    platform = args[0].upper()
    league_id = args[1]
    commish = args[2]
    chat_id = update.effective_chat.id

    if platform not in ["FLEAFLICKER", "SLEEPER"]:
        await update.message.reply_text("❌ Plataforma no soportada. Usa FLEAFLICKER o SLEEPER.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO leagues (chat_id, platform, league_id, commish_handle)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            platform = EXCLUDED.platform,
            league_id = EXCLUDED.league_id,
            commish_handle = EXCLUDED.commish_handle;
    """, (chat_id, platform, league_id, commish))
    conn.commit()
    cursor.close()
    conn.close()

    await update.message.reply_text(f"✅ Liga configurada:\n• Plataforma: {platform}\n• ID Liga: {league_id}\n• Commish: {commish}")

async def set_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sintaxis: /setAlerts <horas_user> <horas_commish>"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("⚠️ Uso: `/setAlerts <horas_user> <horas_commish>` (ej: `/setAlerts 2 8`)", parse_mode="Markdown")
        return

    try:
        user_h = int(args[0])
        commish_h = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Introduce números enteros válidos para las horas.")
        return

    chat_id = update.effective_chat.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE leagues SET user_alert_hours = %s, commish_alert_hours = %s WHERE chat_id = %s", (user_h, commish_h, chat_id))
    conn.commit()
    cursor.close()
    conn.close()

    await update.message.reply_text(f"⏱ Alertas actualizadas: Manager a las {user_h}h / Commish a las {commish_h}h.")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sintaxis: /vincular <fleaflicker_user_id>"""
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("⚠️ Uso: `/vincular <fleaflicker_user_id>`", parse_mode="Markdown")
        return

    platform_id = args[0]
    tg_handle = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
    chat_id = update.effective_chat.id

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_mappings (chat_id, platform_user_id, telegram_handle)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id, platform_user_id) DO UPDATE SET
            telegram_handle = EXCLUDED.telegram_handle;
    """, (chat_id, platform_id, tg_handle))
    conn.commit()
    cursor.close()
    conn.close()

    await update.message.reply_text(f"🔗 Manager `{platform_id}` vinculado a {tg_handle}", parse_mode="Markdown")

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de vinculaciones registradas en esta liga."""
    chat_id = update.effective_chat.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT platform_user_id, telegram_handle FROM user_mappings WHERE chat_id = %s", (chat_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        await update.message.reply_text("No hay managers vinculados en este chat.")
        return

    msg = "📋 **Managers Vinculados:**\n\n"
    for p_id, tg_user in rows:
        msg += f"• ID Fleaflicker: `{p_id}` ➔ {tg_user}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- TRABAJO PERIÓDICO DE MONITOREO (OTC) ---

async def check_otc_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, platform, league_id, commish_handle, user_alert_hours, commish_alert_hours FROM leagues")
    leagues = cursor.fetchall()

    now = datetime.now(timezone.utc)

    for chat_id, platform, league_id, commish_handle, u_hours, c_hours in leagues:
        otc_info = None
        if platform == "FLEAFLICKER":
            otc_info = get_fleaflicker_otc(league_id)

        if not otc_info:
            continue

        pick = otc_info["pick_overall"]
        user_id = otc_info["user_id"]

        # Consultar estado previo del draft en DB
        cursor.execute("SELECT current_pick_overall, otc_user_id, otc_start_time, user_alert_sent, commish_alert_sent FROM draft_state WHERE chat_id = %s", (chat_id,))
        state = cursor.fetchone()

        if not state or state[0] != pick or state[1] != user_id:
            # Nuevo pick/turno detectado: reiniciamos temporizador en DB
            cursor.execute("""
                INSERT INTO draft_state (chat_id, current_pick_overall, otc_user_id, otc_start_time, user_alert_sent, commish_alert_sent)
                VALUES (%s, %s, %s, %s, FALSE, FALSE)
                ON CONFLICT (chat_id) DO UPDATE SET
                    current_pick_overall = EXCLUDED.current_pick_overall,
                    otc_user_id = EXCLUDED.otc_user_id,
                    otc_start_time = EXCLUDED.otc_start_time,
                    user_alert_sent = FALSE,
                    commish_alert_sent = FALSE;
            """, (chat_id, pick, user_id, now))
            conn.commit()

            # Obtener nick de Telegram mapeado
            cursor.execute("SELECT telegram_handle FROM user_mappings WHERE chat_id = %s AND platform_user_id = %s", (chat_id, user_id))
            mapping = cursor.fetchone()
            tg_mention = mapping[0] if mapping else otc_info["user_name"]

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎯 **PICK {pick} OTC**: {tg_mention} es tu turno para seleccionar.",
                parse_mode="Markdown"
            )
        else:
            # Pick en curso: comprobar si ha sobrepasado los tiempos límite
            otc_start = state[2]
            user_alert_sent = state[3]
            commish_alert_sent = state[4]

            elapsed_hours = (now - otc_start).total_seconds() / 3600.0

            cursor.execute("SELECT telegram_handle FROM user_mappings WHERE chat_id = %s AND platform_user_id = %s", (chat_id, user_id))
            mapping = cursor.fetchone()
            tg_mention = mapping[0] if mapping else otc_info["user_name"]

            # Alerta al Usuario (por defecto 2h)
            if elapsed_hours >= u_hours and not user_alert_sent:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 **AVISO OTC**: {tg_mention} llevas más de {u_hours}h en el turno. ¡Elige tu pick!",
                    parse_mode="Markdown"
                )
                cursor.execute("UPDATE draft_state SET user_alert_sent = TRUE WHERE chat_id = %s", (chat_id,))
                conn.commit()

            # Alerta al Comisionado (por defecto 8h)
            if elapsed_hours >= c_hours and not commish_alert_sent:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ **ALERTA COMMISH**: {commish_handle}, el manager {tg_mention} lleva más de {c_hours}h OTC sin seleccionar.",
                    parse_mode="Markdown"
                )
                cursor.execute("UPDATE draft_state SET commish_alert_sent = TRUE WHERE chat_id = %s", (chat_id,))
                conn.commit()

    cursor.close()
    conn.close()

# --- MAIN ---

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Manejadores de comandos
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("setAlerts", set_alerts))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    # Programar verificación en segundo plano (cada 90 segundos)
    if app.job_queue:
        app.job_queue.run_repeating(check_otc_job, interval=90, first=10)

    logger.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()