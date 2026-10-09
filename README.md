# CallerBot

Bot de Telegram que avisa en el grupo de quién está on the clock en un draft de Fleaflicker (NFL, NBA, NHL o MLB). Revisa los drafts cada 5 minutos y, cuando cambia el turno, publica `@nick OTC`.

## Puesta en marcha

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # y pon tu token de BotFather en TELEGRAM_BOT_TOKEN
set -a && source .env && set +a
python -m callerbot.bot
```

En BotFather, ejecuta `/setprivacy` → *Disable* para que el bot lea el texto "who's on the clock" en los grupos (los comandos funcionan igual sin esto).

## Uso en un grupo

Cada canal (un grupo, o cada tema de un grupo con temas) tiene su propia liga. Los comandos son los mismos que los del bot anterior y no distinguen mayúsculas.

1. Añade el bot al grupo.
2. En el tema de la liga: `/setLeague 312835 @commish` (el deporte se detecta solo).
3. `/addTelegramUsers ffUser.@telegram ffUser2.@telegram2` y `/getMissingUsers` para ver quién falta.
4. `/startDraft`. Desde ahí avisa "@nick OTC" en cada cambio de turno, lo recuerda cada 2 h (`/setReminderDuration`) y avisa al commish a las 8 h (`/setNotificationDuration`). `/stopDraft` lo para.
5. `/whosOTC` o escribir "who's on the clock" en cualquier momento.

Otros: `/ping`, `/getChannelID`, `/getTeams`, `/getLeague`, `/getLeagueInfo`, `/unsetLeague`, `/setCommish`, `/help`.

Pendiente: Fantrax, Sleeper, informes de waivers, trades, clasificación y marcadores, `/setLocation`, `/suggest` y `/notifyMyLeagues`.

## Estructura

- `callerbot/bot.py` comandos de Telegram y chequeo periódico
- `callerbot/fleaflicker.py` lectura del draft board (API pública de Fleaflicker)
- `callerbot/storage.py` SQLite con ligas por grupo y nicks por equipo
