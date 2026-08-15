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
    conn.commit()
    cursor.close()
    conn.close()

# --- FUNCIONES AUXILIARES DE FLEAFLICKER ---

def get_fleaflicker_teams_info(league_id: str):
    """
    Obtiene un diccionario ordenado con la información completa de Fleaflicker:
    {
       "team_name": str,
       "owner_identifiers": list[str]
    }
    """
    teams_info = []
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
                        identifiers.add(str(owner.get("displayName")).strip())
                    if owner.get("username"):
                        identifiers.add(str(owner.get("username")).strip())
                
                teams_info.append({
                    "team_name": team_name,
                    "identifiers": [i for i in identifiers]
                })
    except Exception as e:
        logger.error(f"Error en FetchLeagueRosters: {e}")

    return teams_info

# --- HANDLERS DE COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guía de uso del bot."""
    mensaje = (
        "📖 **Guía de Uso del Bot**\n\n"
        "**1. Configuración de Ligas**\n"
        "• `/setLeague <league_id> <@comisionado>` - Asocia una liga al chat.\n"
        "• `/desvincularLiga` - Desvincula la liga actual del chat.\n\n"
        "**2. Vinculación de Managers**\n"
        "• `/vincular <fleaflicker_id>`\n"
        "  Vincula tu ID/Nick de Fleaflicker a tu usuario de Telegram y detecta el nombre de tu equipo.\n"
        "  _Ejemplo:_ `/vincular Las1001`\n\n"
        "• `/vincular <fleaflicker_id> <@telegram_nick>`\n"
        "  (Admin) Vincula a un manager especificando su ID de Fleaflicker y su Telegram.\n"
        "  _Ejemplo:_ `/vincular Las1001 @daovir`\n\n"
        "• `/managers` - Muestra la relación entre Equipos, Fleaflicker IDs y Nicks de Telegram."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! El bot está listo. Usa `/help` para ver las instrucciones.",
        parse_mode="Markdown"
    )

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setLeague <league_id> <commish_handle>`", parse_mode="Markdown")
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

    await update.message.reply_text(f"✅ Liga `{league_id}` configurada correctamente.", parse_mode="Markdown")

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
            await update.message.reply_text("🗑️ Liga desvinculada de este chat.")
        else:
            await update.message.reply_text("⚠️ No había ninguna liga vinculada.")
    except Exception as e:
        logger.error(f"Error en /desvincularLiga: {e}")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vincula Fleaflicker ID + Nombre del Equipo + Telegram Nick."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Uso: `/vincular <id_fleaflicker>` o `/vincular <id_fleaflicker> <@telegram_nick>`", parse_mode="Markdown")
        return

    # 1. Obtener la liga configurada en el chat
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
    league_row = cursor.fetchone()
    
    if not league_row:
        cursor.close()
        conn.close()
        await update.message.reply_text("⚠️ Configura primero la liga en este chat con `/setLeague <league_id> <@comisionado>`")
        return

    league_id = league_row[0]

    # 2. Parsear argumentos
    if len(context.args) >= 2 and context.args[-1].startswith("@"):
        handle = context.args[-1].strip()
        fleaflicker_id = " ".join(context.args[:-1]).strip()
        telegram_id = None
    else:
        fleaflicker_id = " ".join(context.args).strip()
        handle = f"@{user.username}" if user.username else f"@{user.first_name}"
        telegram_id = user.id

    # 3. Buscar el nombre del equipo correspondiente al ID de Fleaflicker
    teams = get_fleaflicker_teams_info(league_id)
    detected_team_name = "Desconocido (No encontrado en la liga)"

    for team in teams:
        for ident in team["identifiers"]:
            if ident.lower() == fleaflicker_id.lower():
                detected_team_name = team["team_name"]
                break

    # 4. Guardar la triple relación en PostgreSQL
    cursor.execute("""
        INSERT INTO user_mappings (fleaflicker_id, team_name, telegram_handle, telegram_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (fleaflicker_id) 
        DO UPDATE SET 
            team_name = EXCLUDED.team_name,
            telegram_handle = EXCLUDED.telegram_handle, 
            telegram_id = COALESCE(EXCLUDED.telegram_id, user_mappings.telegram_id);
    """, (fleaflicker_id, detected_team_name, handle, telegram_id))

    conn.commit()
    cursor.close()
    conn.close()

    await update.message.reply_text(
        f"✅ **Vinculación Registrada:**\n\n"
        f"• **ID Fleaflicker:** `{fleaflicker_id}`\n"
        f"• **Equipo:** {detected_team_name}\n"
        f"• **Telegram:** {handle}",
        parse_mode="Markdown"
    )

async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de vinculaciones guardadas."""
    chat_id = update.effective_chat.id

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text("⚠️ Configura primero la liga con `/setLeague`.")
            return

        league_id = league_row[0]
        cursor.execute("SELECT fleaflicker_id, team_name, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        teams = get_fleaflicker_teams_info(league_id)
        
        # Mapeo indexado por identificador
        db_map = {}
        for f_id, t_name, t_handle in mappings:
            db_map[f_id.lower()] = (t_name, t_handle)

        texto = f"📋 **Relación de Managers (`Liga {league_id}`)**\n\n"

        for team in teams:
            t_name = team["team_name"]
            matched_handle = None
            matched_fid = None

            for ident in team["identifiers"]:
                if ident.lower() in db_map:
                    matched_fid = ident
                    _, matched_handle = db_map[ident.lower()]
                    break

            if matched_handle:
                texto += f"🏈 **{t_name}**\n├ 🆔 Fleaflicker: `{matched_fid}`\n└ 👤 Telegram: {matched_handle}\n\n"
            else:
                texto += f"🏈 **{t_name}**\n└ ❌ *Sin vincular*\n\n"

        await update.message.reply_text(texto, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error en /managers: {e}")
        await update.message.reply_text("❌ Error al obtener la lista de managers.")

# --- MAIN ---

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setLeague", set_league))
    app.add_handler(CommandHandler("desvincularLiga", desvincular_liga))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    logger.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()