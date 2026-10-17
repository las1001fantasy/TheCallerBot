"""Textos del bot en castellano (por defecto) y catalán. Cada liga guarda su idioma ("es" o "ca")."""

from __future__ import annotations

LANGS = ("es", "ca")
# Palabras que, al final de /setLeague, eligen el idioma de las respuestas en ese canal.
LANG_WORDS = {"CAT": "ca", "CA": "ca", "ES": "es", "CAST": "es"}

HELP_ES = """CallerBot avisa de quién está on the clock en los drafts de Fleaflicker.

Comandos públicos:
/ping - Comprueba que el bot está funcionando
/setLeague leagueID @commish (opcional) NFL|NBA|NHL|MLB (opcional) CAT (opcional) - Asigna la liga de Fleaflicker a este canal. El leagueID es el último número de la URL de la liga, p. ej. fleaflicker.com/nfl/leagues/123456. Con CAT al final, el bot responde en catalán en este canal
/getChannelID - Devuelve el ID del canal
/getTeams - Lista los equipos con su usuario de Fleaflicker y de Telegram
/whosOTC - Quién está on the clock (también vale escribir "who's on the clock")
/getIllegalRosters - Revisa las plantillas: lesionados (OUT, IR, PUP, EX...) de titular, sanos en IR, no rookies en TAXI y huecos vacíos (las Q no se avisan)
/getLeagueStanding - Clasificación de la liga
/getScoreBoard (jornada) - Resultados de la jornada actual, o de la que indiques (ligas H2H)

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
/setNotificationDuration horas - A las cuántas horas de OTC se avisa al commish (1-23, por defecto 8)
/enableStandingNotification y /disableStandingNotification - Activa o quita la clasificación semanal
/enableScoreboardNotification y /disableScoreboardNotification - Activa o quita los resultados semanales
Los informes semanales salen el martes a las 10h en la NFL y el lunes a las 10h en NBA, NHL y MLB.

De 00h a 08h (hora de España) el reloj del OTC se para: esas horas no cuentan, no hay recordatorios ni avisos al commish, y solo se anuncia el nuevo OTC si alguien hace un pick."""

HELP_CA = """CallerBot avisa de qui està on the clock als drafts de Fleaflicker.

Ordres públiques:
/ping - Comprova que el bot funciona
/setLeague leagueID @commish (opcional) NFL|NBA|NHL|MLB (opcional) CAT (opcional) - Assigna la lliga de Fleaflicker a aquest canal. El leagueID és l'últim número de l'URL de la lliga, p. ex. fleaflicker.com/nfl/leagues/123456. Amb CAT al final, el bot respon en català en aquest canal
/getChannelID - Retorna l'ID del canal
/getTeams - Llista els equips amb el seu usuari de Fleaflicker i de Telegram
/whosOTC - Qui està on the clock (també val escriure "who's on the clock")
/getIllegalRosters - Revisa les plantilles: lesionats (OUT, IR, PUP, EX...) de titular, sans a l'IR, no rookies al TAXI i forats buits (les Q no s'avisen)
/getLeagueStanding - Classificació de la lliga
/getScoreBoard (jornada) - Resultats de la jornada actual, o de la que indiquis (lligues H2H)

Gestió de la lliga (commish o admins del grup):
/getLeague - Lliga assignada a aquest canal
/getLeagueInfo - Informació de la lliga i del draft
/getMissingUsers - Usuaris de Fleaflicker sense usuari de Telegram
/unsetLeague - Treu la lliga del canal
/setCommish @commish - Canvia el commish
/addTelegramUsers ffUser.@telegram ffUser2.@telegram2 ... - Associa usuaris de Fleaflicker amb Telegram (sense espais dins dels noms)
/startDraft - Comença a avisar de qui està OTC
/stopDraft - Deixa d'avisar
/setReminderDuration hores - Cada quantes hores es recorda qui està OTC (1-23, per defecte 2)
/setNotificationDuration hores - A les quantes hores d'OTC s'avisa el commish (1-23, per defecte 8)
/enableStandingNotification i /disableStandingNotification - Activa o treu la classificació setmanal
/enableScoreboardNotification i /disableScoreboardNotification - Activa o treu els resultats setmanals
Els informes setmanals surten dimarts a les 10h a la NFL i dilluns a les 10h a NBA, NHL i MLB.

De 00h a 08h (hora d'Espanya) el rellotge de l'OTC s'atura: aquestes hores no compten, no hi ha recordatoris ni avisos al commish, i només s'anuncia el nou OTC si algú fa un pick."""

TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "help": HELP_ES,
        "no_league": "No hay liga en este canal. Usa /setLeague primero.",
        "only_manager": "Solo el commish o un admin del grupo pueden usar este comando.",
        "draft_over": "El draft ha terminado.",
        "otc_line": "{who} OTC\nRonda {round}, pick {slot} (#{overall})",
        "ping": "pong, estoy funcionando.",
        "channel_id": "Channel ID: {chat_id}",
        "topic": " · Tema: {thread}",
        "setleague_usage": "Uso: /setLeague leagueID @commish (opcional) NFL|NBA|NHL|MLB (opcional) CAT (opcional)",
        "already_set": "Este canal ya tiene liga; solo el commish o un admin pueden cambiarla.",
        "cant_read": "No he podido leer la liga: {error}",
        "league_set": "Liga {league_id} ({sport}) asignada a este canal{commish}. {teams} equipos.\n"
                      "Asocia los usuarios con /addTelegramUsers y empieza con /startDraft.",
        "commish_part": ", commish {commish}",
        "ff_error": "Error leyendo Fleaflicker: {error}",
        "teams_header": "Equipos de la liga {league_id} (Fleaflicker):",
        "no_owner": "sin dueño",
        "no_telegram": "sin Telegram",
        "checking_rosters": "Revisando las plantillas, tarda unos segundos...",
        "rosters_header": "Plantillas con incidencias:",
        "rosters_ok": "Todas las plantillas están en regla.",
        "rosters_failed": "No he podido revisar:",
        "league_short": "Liga {league_id} ({sport})",
        "league_info": "Liga {league_id} ({sport})\nCommish: {commish}\nEquipos: {teams} · Picks hechos: {picks}\n"
                       "OTC: {otc}\nAvisos: {alerts} · recordatorio cada {reminder} h · commish a las {notify} h",
        "unassigned": "sin asignar",
        "draft_finished_short": "draft terminado",
        "alerts_on": "activados",
        "alerts_off": "parados",
        "all_mapped": "Todos los usuarios de Fleaflicker tienen Telegram asociado.",
        "missing_header": "Sin usuario de Telegram:",
        "orphan_header": "Equipos sin dueño en Fleaflicker (asócialos por nombre, sin espacios: NombreEquipo.@telegram):",
        "unset": "Liga {league_id} quitada de este canal.",
        "setcommish_usage": "Uso: /setCommish @commish",
        "commish_set": "Commish: {commish}",
        "add_usage": "Uso: /addTelegramUsers ffUser.@telegram ffUser2.@telegram2 ...",
        "added": "Añadidos:",
        "not_understood": "No entiendo:",
        "draft_started": "Draft en marcha. Avisaré en este canal cada vez que cambie el OTC.",
        "draft_stopped": "Avisos de OTC parados. /startDraft para reanudarlos.",
        "reminder_usage": "Uso: /setReminderDuration horas (1-23)",
        "reminder_set": "Recordaré quién está OTC cada {hours} h.",
        "notify_usage": "Uso: /setNotificationDuration horas (1-23)",
        "notify_set": "Avisaré al commish cuando alguien lleve {hours} h OTC.",
        "draft_finished": "El draft ha terminado. ¡Suerte a todos!",
        "still_otc": "{who} sigue OTC (desde hace {hours:.0f} h)",
        "commish_alert": "{commish}: {team} lleva {hours:.0f} h OTC.",
        "issue_empty": "hueco vacío en {where}",
        "issue_injured_starter": "{name} ({status}) de titular en {where}",
        "issue_healthy_ir": "{name} está sano{status} y ocupa un hueco de IR",
        "issue_taxi": "{name} no es rookie y está en TAXI",
        "standings_header": "Clasificación de la liga {league_id}:",
        "scoreboard_header": "Resultados de la jornada {week}:",
        "scoreboard_header_noweek": "Resultados de la jornada:",
        "in_progress": "en juego",
        "scoreboard_usage": "Uso: /getScoreBoard (jornada opcional, p. ej. /getScoreBoard 5)",
        "standings_on": "Clasificación semanal activada ({when}).",
        "standings_off": "Clasificación semanal desactivada.",
        "scoreboard_on": "Resultados semanales activados ({when}).",
        "scoreboard_off": "Resultados semanales desactivados.",
        "when_nfl": "martes a las 10h",
        "when_other": "lunes a las 10h",
        "weekly_title": "📊 Informe semanal",
    },
    "ca": {
        "help": HELP_CA,
        "no_league": "No hi ha cap lliga en aquest canal. Fes servir /setLeague primer.",
        "only_manager": "Només el commish o un admin del grup poden fer servir aquesta ordre.",
        "draft_over": "El draft s'ha acabat.",
        "otc_line": "{who} OTC\nRonda {round}, pick {slot} (#{overall})",
        "ping": "pong, estic funcionant.",
        "channel_id": "Channel ID: {chat_id}",
        "topic": " · Tema: {thread}",
        "setleague_usage": "Ús: /setLeague leagueID @commish (opcional) NFL|NBA|NHL|MLB (opcional) CAT (opcional)",
        "already_set": "Aquest canal ja té lliga; només el commish o un admin la poden canviar.",
        "cant_read": "No he pogut llegir la lliga: {error}",
        "league_set": "Lliga {league_id} ({sport}) assignada a aquest canal{commish}. {teams} equips.\n"
                      "Associa els usuaris amb /addTelegramUsers i comença amb /startDraft.",
        "commish_part": ", commish {commish}",
        "ff_error": "Error llegint Fleaflicker: {error}",
        "teams_header": "Equips de la lliga {league_id} (Fleaflicker):",
        "no_owner": "sense propietari",
        "no_telegram": "sense Telegram",
        "checking_rosters": "Revisant les plantilles, triga uns segons...",
        "rosters_header": "Plantilles amb incidències:",
        "rosters_ok": "Totes les plantilles estan en regla.",
        "rosters_failed": "No he pogut revisar:",
        "league_short": "Lliga {league_id} ({sport})",
        "league_info": "Lliga {league_id} ({sport})\nCommish: {commish}\nEquips: {teams} · Picks fets: {picks}\n"
                       "OTC: {otc}\nAvisos: {alerts} · recordatori cada {reminder} h · commish a les {notify} h",
        "unassigned": "sense assignar",
        "draft_finished_short": "draft acabat",
        "alerts_on": "activats",
        "alerts_off": "aturats",
        "all_mapped": "Tots els usuaris de Fleaflicker tenen Telegram associat.",
        "missing_header": "Sense usuari de Telegram:",
        "orphan_header": "Equips sense propietari a Fleaflicker (associa'ls pel nom, sense espais: NomEquip.@telegram):",
        "unset": "Lliga {league_id} treta d'aquest canal.",
        "setcommish_usage": "Ús: /setCommish @commish",
        "commish_set": "Commish: {commish}",
        "add_usage": "Ús: /addTelegramUsers ffUser.@telegram ffUser2.@telegram2 ...",
        "added": "Afegits:",
        "not_understood": "No ho entenc:",
        "draft_started": "Draft en marxa. Avisaré en aquest canal cada cop que canviï l'OTC.",
        "draft_stopped": "Avisos d'OTC aturats. /startDraft per reprendre'ls.",
        "reminder_usage": "Ús: /setReminderDuration hores (1-23)",
        "reminder_set": "Recordaré qui està OTC cada {hours} h.",
        "notify_usage": "Ús: /setNotificationDuration hores (1-23)",
        "notify_set": "Avisaré el commish quan algú porti {hours} h OTC.",
        "draft_finished": "El draft s'ha acabat. Sort a tothom!",
        "still_otc": "{who} continua OTC (des de fa {hours:.0f} h)",
        "commish_alert": "{commish}: {team} porta {hours:.0f} h OTC.",
        "issue_empty": "forat buit a {where}",
        "issue_injured_starter": "{name} ({status}) de titular a {where}",
        "issue_healthy_ir": "{name} està sa{status} i ocupa un forat d'IR",
        "issue_taxi": "{name} no és rookie i està al TAXI",
        "standings_header": "Classificació de la lliga {league_id}:",
        "scoreboard_header": "Resultats de la jornada {week}:",
        "scoreboard_header_noweek": "Resultats de la jornada:",
        "in_progress": "en joc",
        "scoreboard_usage": "Ús: /getScoreBoard (jornada opcional, p. ex. /getScoreBoard 5)",
        "standings_on": "Classificació setmanal activada ({when}).",
        "standings_off": "Classificació setmanal desactivada.",
        "scoreboard_on": "Resultats setmanals activats ({when}).",
        "scoreboard_off": "Resultats setmanals desactivats.",
        "when_nfl": "dimarts a les 10h",
        "when_other": "dilluns a les 10h",
        "weekly_title": "📊 Informe setmanal",
    },
}


def t(lang: str | None, key: str, **kw) -> str:
    texts = TEXTS.get(lang or "es", TEXTS["es"])
    return texts.get(key, TEXTS["es"][key]).format(**kw)
