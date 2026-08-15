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

    # Ajustes estructurales de migración automática
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

def get_fleaflicker_teams_info(league_id: str):
    """
    Obtiene todos los equipos de la liga desde Fleaflicker junto con 
    todos sus posibles identificadores (ID de equipo, ID de usuario, DisplayName, Username).
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
                
                # 1. ID propio del equipo
                if team.get("id"):
                    identifiers.add(str(team.get("id")).strip())
                
                # 2. Iterar sobre propietarios (owners) y extraer de obj 'user' embebido
                for owner in team.get("owners", []):
                    # Directos
                    if owner.get("id"):
                        identifiers.add(str(owner.get("id")).strip())
                    if owner.get("displayName"):
                        identifiers.add(str(owner.get("displayName")).strip())
                    if owner.get("username"):
                        identifiers.add(str(owner.get("username")).strip())
                    
                    # Objeto 'user' anidado (Estructura real de Fleaflicker API)
                    user_obj = owner.get("user", {})
                    if user_obj.get("id"):
                        identifiers.add(str(user_obj.get("id")).strip())
                    if user_obj.get("displayName"):
                        identifiers.add(str(user_obj.get("displayName")).strip())
                    if user_obj.get("username"):
                        identifiers.add(str(user_obj.get("username")).strip())
                    if user_obj.get("name"):
                        identifiers.add(str(user_obj.get("name")).strip())

                teams_info.append({
                    "team_name": team_name,
                    "identifiers": [i for i in identifiers if i]
                })
        else:
            logger.error(f"Fleaflicker API Error HTTP {res.status_code}")
    except Exception as e:
        logger.error(f"Error en FetchLeagueRosters: {e}")

    return teams_info

# --- HANDLERS DE COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guía completa de instrucciones y comandos del bot."""
    mensaje = (
        "📖 **Guía de Uso del Bot**\n\n"
        "**1. Configuración de Ligas**\n"
        "• `/setLeague <league_id> <@comisionado>`\n"
        "  Asocia una liga de Fleaflicker a este chat.\n\n"
        "• `/desvincularLiga`\n"
        "  Elimina la liga configurada en este chat actual.\n\n"
        "• `/testLeague`\n"
        "  Prueba la conexión con Fleaflicker y muestra los equipos detectados.\n\n"
        "• `/setAlerts <horas_user> <horas_commish>`\n"
        "  Ajusta el tiempo límite antes de enviar alertas en el draft.\n\n"
        "**2. Gestión de Managers y Equipos**\n"
        "• `/vincular <id_fleaflicker>`\n"
        "  Vincula tu ID/Username de Fleaflicker con tu usuario de Telegram.\n"
        "  _Ejemplo:_ `/vincular Las1001`\n\n"
        "• `/vincular <id_fleaflicker> <@telegram_nick>`\n"
        "  (Commish) Vincula a otro usuario con su nick de Telegram.\n"
        "  _Ejemplo:_ `/vincular Las1001 @daovir`\n\n"
        "• `/managers`\n"
        "  Muestra el estado de vinculación de los equipos de la liga."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /start."""
    await update.message.reply_text(
        "¡Hola! El bot está iniciado y listo.\n\n"
        "Usa `/help` para ver la lista de comandos disponibles.",
        parse_mode="Markdown"
    )

async def set_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configura la liga de Fleaflicker asociada a este chat."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setLeague <league_id> <commish_handle>`", parse_mode="Markdown")
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

        await update.message.reply_text(f"✅ Liga `{league_id}` configurada correctamente para este chat.", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en /setLeague: {e}")
        await update.message.reply_text("❌ Error al guardar la liga.")

async def test_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de depuración para probar la extracción de Fleaflicker."""
    chat_id = update.effective_chat.id
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not league_row:
            await update.message.reply_text("⚠️ No hay liga asociada a este chat. Usa `/setLeague <league_id> <@commish>` primero.")
            return

        league_id = league_row[0]
        teams = get_fleaflicker_teams_info(league_id)

        if not teams:
            await update.message.reply_text(f"❌ No se pudieron obtener datos para la Liga `{league_id}` de Fleaflicker.")
            return

        out = f"🔍 **Equipos e Identificadores en Liga `{league_id}`:**\n\n"
        for t in teams:
            out += f"🏈 **{t['team_name']}**\nIDs/Users: `{', '.join(t['identifiers'])}`\n\n"

        await update.message.reply_text(out, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error en /testLeague: {e}")
        await update.message.reply_text(f"❌ Error al consultar Fleaflicker: `{e}`", parse_mode="Markdown")

async def desvincular_liga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina la vinculación de la liga en el chat actual."""
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
            await update.message.reply_text("🗑️ **Liga desvinculada.** Puedes volver a configurar otra con `/setLeague`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ No hay ninguna liga configurada en este chat.")
    except Exception as e:
        logger.error(f"Error en /desvincularLiga: {e}")
        await update.message.reply_text("❌ Hubo un error al desvincular la liga.")

async def set_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ajusta el tiempo de alerta para el manager OTC y el comisionado."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/setAlerts <horas_user> <horas_commish>`", parse_mode="Markdown")
        return

    try:
        user_hours = int(context.args[0])
        commish_hours = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Ingresa números válidos para las horas.")
        return

    try:
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

        await update.message.reply_text(f"⏰ Alertas actualizadas: Usuario a las {user_hours}h, Comisionado a las {commish_hours}h.")
    except Exception as e:
        logger.error(f"Error en /setAlerts: {e}")

async def vincular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vincula Fleaflicker ID + Nombre del Equipo + Telegram Nick."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/vincular <id_fleaflicker>` o `/vincular <id_fleaflicker> <@telegram_nick>`", parse_mode="Markdown")
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT league_id FROM leagues WHERE chat_id = %s;", (chat_id,))
        league_row = cursor.fetchone()

        if not league_row:
            cursor.close()
            conn.close()
            await update.message.reply_text(
                "⚠️ **No hay liga vinculada a este chat.**\n\n"
                "Ejecuta primero:\n`/setLeague <league_id> <@comisionado>`",
                parse_mode="Markdown"
            )
            return

        league_id = league_row[0]

        # Parsear argumentos
        if len(context.args) >= 2 and context.args[-1].startswith("@"):
            handle = context.args[-1].strip()
            fleaflicker_id = " ".join(context.args[:-1]).strip()
            telegram_id = None
        else:
            fleaflicker_id = " ".join(context.args).strip()
            handle = f"@{user.username}" if user.username else f"@{user.first_name}"
            telegram_id = user.id

        # Buscar si el ID/Username de Fleaflicker pertenece a un equipo
        teams = get_fleaflicker_teams_info(league_id)
        detected_team_name = None

        for team in teams:
            for ident in team["identifiers"]:
                if ident.lower() == fleaflicker_id.lower():
                    detected_team_name = team["team_name"]
                    break
            if detected_team_name:
                break

        if not detected_team_name:
            team_display = "Sin equipo asignado (Solo Manager/Commish)"
        else:
            team_display = detected_team_name

        cursor.execute("""
            INSERT INTO user_mappings (fleaflicker_id, team_name, telegram_handle, telegram_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fleaflicker_id) 
            DO UPDATE SET 
                team_name = EXCLUDED.team_name,
                telegram_handle = EXCLUDED.telegram_handle, 
                telegram_id = COALESCE(EXCLUDED.telegram_id, user_mappings.telegram_id);
        """, (fleaflicker_id, team_display, handle, telegram_id))

        conn.commit()
        cursor.close()
        conn.close()

        await update.message.reply_text(
            f"✅ **Vinculación Registrada:**\n\n"
            f"• **ID Fleaflicker:** `{fleaflicker_id}`\n"
            f"• **Equipo:** {team_display}\n"
            f"• **Telegram:** {handle}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error en /vincular: {e}")
        await update.message.reply_text(f"❌ Error al realizar la vinculación: `{e}`", parse_mode="Markdown")

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
            await update.message.reply_text("⚠️ Configura primero la liga en este chat con `/setLeague`.")
            return

        league_id = league_row[0]
        cursor.execute("SELECT fleaflicker_id, team_name, telegram_handle FROM user_mappings;")
        mappings = cursor.fetchall()
        cursor.close()
        conn.close()

        teams = get_fleaflicker_teams_info(league_id)
        
        db_map = {f_id.lower(): (t_name, t_handle) for f_id, t_name, t_handle in mappings}

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
    app.add_handler(CommandHandler("testLeague", test_league))
    app.add_handler(CommandHandler("setAlerts", set_alerts))
    app.add_handler(CommandHandler("vincular", vincular))
    app.add_handler(CommandHandler("managers", list_managers))

    logger.info("Bot iniciado correctamente...")
    app.run_polling()

if __name__ == "__main__":
    main()