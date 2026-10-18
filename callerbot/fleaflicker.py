"""Lectura del draft board de Fleaflicker (API pública, sin autenticación)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from .i18n import t

API = "https://www.fleaflicker.com/api"
SPORTS = ("NFL", "NBA", "NHL", "MLB")


class FleaflickerError(Exception):
    pass


@dataclass
class Team:
    id: int
    name: str
    owners: list[str] = field(default_factory=list)
    slot: int | None = None  # posición en la ronda 1


@dataclass
class Pick:
    overall: int
    round: int
    slot: int
    team: Team


@dataclass
class DraftState:
    teams: list[Team]
    on_the_clock: Pick | None  # None = draft terminado
    picks_made: int


async def fetch_board(client: httpx.AsyncClient, league_id: int, sport: str | None) -> dict:
    params = {"league_id": league_id}
    if sport:
        params["sport"] = sport
    r = await client.get(f"{API}/FetchLeagueDraftBoard", params=params, timeout=20)
    if r.status_code != 200:
        raise FleaflickerError(f"Fleaflicker respondió {r.status_code}")
    data = r.json()
    if not data.get("orderedSelections") and not data.get("rows"):
        raise FleaflickerError("La liga no tiene draft board (¿ID o deporte incorrecto?)")
    return data


async def detect_sport(client: httpx.AsyncClient, league_id: int) -> str:
    """Busca primero un draft board en cada deporte y, si no hay ninguno, una clasificación.

    El mismo ID puede existir en varios deportes, así que el draft board manda; para evitar dudas
    se puede indicar el deporte en /setLeague (p. ej. /setLeague 30332 NBA).
    """
    for sport in SPORTS:
        try:
            await fetch_board(client, league_id, sport)
            return sport
        except (FleaflickerError, httpx.HTTPError, ValueError):
            pass
    for sport in SPORTS:
        try:
            if await _standings_teams(client, league_id, sport):
                return sport
        except (httpx.HTTPError, ValueError):
            pass
    raise FleaflickerError(f"No encuentro la liga {league_id} en Fleaflicker")


def _team(raw: dict) -> Team:
    owners: list[str] = []
    for o in raw.get("owners", []):
        user = o.get("user") or {}
        for name in (o.get("displayName"), o.get("username"), user.get("displayName"), user.get("username")):
            if name and name not in owners:
                owners.append(name)
    return Team(id=int(raw["id"]), name=raw.get("name") or raw.get("nickname") or f"Equipo {raw['id']}", owners=owners)


def _cell_team(cell: dict) -> dict | None:
    """El equipo de una celda puede venir en varios sitios (así lo busca también TheCallerBot)."""
    for t in (cell.get("claimTeam"), cell.get("team"), (cell.get("roster") or {}).get("team"), (cell.get("slot") or {}).get("team")):
        if t and t.get("id") is not None:
            return t
    return None


def _selections(data: dict) -> list[dict]:
    """Devuelve los picks en orden con el formato de orderedSelections, venga como venga el board."""
    if data.get("orderedSelections"):
        return data["orderedSelections"]
    out: list[dict] = []
    for r_idx, row in enumerate(data.get("rows", []), start=1):
        cells = row.get("cells", [])
        for c_idx, cell in enumerate(cells, start=1):
            team = _cell_team(cell)
            if not team:
                continue
            slot = dict(cell.get("slot") or {})
            slot.setdefault("round", r_idx)
            slot.setdefault("slot", c_idx)
            slot.setdefault("overall", cell.get("overall") or (r_idx - 1) * len(cells) + c_idx)
            out.append({**cell, "team": team, "slot": slot})
    return sorted(out, key=lambda s: int(s["slot"]["overall"]))


def parse_board(data: dict) -> DraftState:
    selections = _selections(data)
    teams: dict[int, Team] = {}
    picks: list[tuple[Pick, dict]] = []

    for sel in selections:
        if "team" not in sel:
            continue
        team = teams.setdefault(int(sel["team"]["id"]), _team(sel["team"]))
        slot = sel.get("slot", {})
        pick = Pick(
            overall=int(slot.get("overall", len(picks) + 1)),
            round=int(slot.get("round", 0)),
            slot=int(slot.get("slot", 0)),
            team=team,
        )
        if pick.round == 1 and team.slot is None:
            team.slot = pick.slot
        picks.append((pick, sel))

    # Si Fleaflicker marca el turno explícitamente lo usamos; si no, es el primer pick sin jugador.
    otc = next((p for p, s in picks if s.get("isOnTheClock")), None)
    if otc is None:
        otc = next((p for p, s in picks if not s.get("player")), None)

    made = sum(1 for _, s in picks if s.get("player"))
    ordered = sorted(teams.values(), key=lambda t: (t.slot is None, t.slot or 0, t.name))
    return DraftState(teams=ordered, on_the_clock=otc, picks_made=made)


async def _standings_teams(client: httpx.AsyncClient, league_id: int, sport: str | None) -> list[Team]:
    params = {"league_id": league_id}
    if sport:
        params["sport"] = sport
    r = await client.get(f"{API}/FetchLeagueStandings", params=params, timeout=20)
    data = r.json() if r.status_code == 200 else {}
    teams = [_team(t) for d in data.get("divisions", []) for t in d.get("teams", []) if t.get("id") is not None]
    return sorted(teams, key=lambda t: t.name)


# Los dueños de los equipos casi nunca cambian: los guardamos unas horas para no pedirlos en cada revisión.
OWNERS_TTL = 6 * 3600
_owners_cache: dict[tuple[int, str | None], tuple[float, dict[int, list[str]]]] = {}


async def fetch_owners(client: httpx.AsyncClient, league_id: int, sport: str | None) -> dict[int, list[str]]:
    key = (league_id, sport)
    cached = _owners_cache.get(key)
    if cached and time.time() - cached[0] < OWNERS_TTL:
        return cached[1]
    owners = await _fetch_owners(client, league_id, sport)
    if owners:
        _owners_cache[key] = (time.time(), owners)
    return owners


async def _fetch_owners(client: httpx.AsyncClient, league_id: int, sport: str | None) -> dict[int, list[str]]:
    """Dueños de cada equipo. El draft board no siempre los trae; la clasificación y las plantillas sí."""
    params = {"league_id": league_id}
    if sport:
        params["sport"] = sport
    owners: dict[int, list[str]] = {}
    for endpoint in ("FetchLeagueStandings", "FetchLeagueRosters"):
        try:
            r = await client.get(f"{API}/{endpoint}", params=params, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
        except (httpx.HTTPError, ValueError):
            continue
        raw_teams = [t for d in data.get("divisions", []) for t in d.get("teams", [])]
        raw_teams += [ro.get("team", {}) for ro in data.get("rosters", [])]
        for raw in raw_teams:
            if raw.get("id") is None:
                continue
            team = _team(raw)
            if team.owners:
                owners.setdefault(team.id, team.owners)
        if owners:
            break
    return owners


async def fetch_state(client: httpx.AsyncClient, league_id: int, sport: str | None) -> DraftState:
    try:
        state = parse_board(await fetch_board(client, league_id, sport))
    except FleaflickerError:
        # Liga ya en temporada sin draft board: sacamos los equipos de la clasificación.
        owners = await fetch_owners(client, league_id, sport)
        if not owners:
            raise
        teams = await _standings_teams(client, league_id, sport)
        return DraftState(teams=teams, on_the_clock=None, picks_made=0)
    if any(not t.owners for t in state.teams):
        owners = await fetch_owners(client, league_id, sport)
        for t in state.teams:
            if not t.owners:
                t.owners = owners.get(t.id, [])
    return state


# ---------- Revisión de alineaciones ----------

# Estados que no deberían estar en la alineación titular. Las Q (questionable) no se avisan a propósito.
BAD_STATUS = {"O", "OUT", "IR", "PUP", "NFI", "EX", "SUSP", "SUS", "D", "DOUBTFUL", "INJURED_RESERVE", "SUSPENDED"}
OK_STATUS = {"", "Q", "QUESTIONABLE", "P", "PROBABLE", "GTD", "DTD", "ACTIVE", "HEALTHY"}


async def fetch_roster(client: httpx.AsyncClient, league_id: int, sport: str | None, team_id: int) -> dict:
    params = {"league_id": league_id, "team_id": team_id}
    if sport:
        params["sport"] = sport
    r = await client.get(f"{API}/FetchRoster", params=params, timeout=20)
    if r.status_code != 200:
        raise FleaflickerError(f"Fleaflicker respondió {r.status_code}")
    return r.json()


def _statuses(pro: dict) -> list[str]:
    """Todos los valores de lesión que da Fleaflicker (abreviatura, gravedad, nombre completo), en mayúsculas."""
    injury = pro.get("injury") or {}
    values = []
    # Fleaflicker escribe "typeAbbreviaition" (con la errata); probamos también la forma correcta.
    for key in ("typeAbbreviaition", "typeAbbreviation", "severity", "typeFull"):
        if injury.get(key):
            values.append(str(injury[key]).upper().replace(" ", "_"))
    return values


def _status(pro: dict) -> str:
    values = _statuses(pro)
    bad = [v for v in values if v in BAD_STATUS or v.startswith("OUT")]
    return (bad or values or [""])[0].replace("_", " ")


def _is_bad(pro: dict) -> bool:
    """Lesionado de verdad: basta con que uno de los campos diga OUT, IR, etc. (antes solo mirábamos el primero)."""
    return any(v in BAD_STATUS or v.startswith("OUT") for v in _statuses(pro))


def _is_rookie(pro: dict) -> bool | None:
    for key in ("isRookie", "rookie"):
        if key in pro:
            return bool(pro[key])
    for key in ("yearsOfExperience", "experience", "yearsExperience"):
        if key in pro:
            return int(pro[key] or 0) == 0
    return None  # no sabemos


def _on_bye(lp: dict, pro: dict, week: int | None) -> bool:
    """Titular cuyo equipo descansa esta semana (NFL): por la semana de bye o porque el partido pedido es un bye."""
    for g in lp.get("requestedGames") or []:
        if g.get("isBye") or (g.get("game") or {}).get("isBye"):
            return True
    bye = pro.get("nflByeWeek") or pro.get("byeWeek")
    return bool(week and bye and int(bye) == int(week))


def roster_issues(data: dict, lang: str = "es", week: int | None = None) -> list[str]:
    """Problemas de una plantilla: lesionados o de bye de titular, sanos en IR, no rookies en TAXI, huecos vacíos."""
    issues: list[str] = []
    period = data.get("schedulePeriod") or data.get("scoringPeriod") or {}
    week = week or (period.get("ordinal") if isinstance(period, dict) else None)
    for group in data.get("groups", []):
        gname = str(group.get("group") or group.get("label") or "").upper()
        for slot in group.get("slots", []):
            pos = slot.get("position") or {}
            label = str(pos.get("label") or "").upper()
            where = label or gname
            lp = slot.get("leaguePlayer") or {}
            pro = lp.get("proPlayer") or {}
            starter = gname in ("START", "STARTER", "STARTERS")
            in_ir = gname in ("INJURED", "INJURED_RESERVE") or label in ("IR", "INJ")
            in_taxi = gname == "TAXI" or label == "TAXI"
            if not pro:
                if starter:
                    issues.append(t(lang, "issue_empty", where=where))
                continue
            name = pro.get("nameFull") or pro.get("nameShort") or "?"
            status = _status(pro)
            if starter and _is_bad(pro):
                issues.append(t(lang, "issue_injured_starter", name=name, status=status, where=where))
            elif starter and _on_bye(lp, pro, week):
                issues.append(t(lang, "issue_bye", name=name, where=where))
            elif in_ir and not _is_bad(pro) and status in OK_STATUS:
                issues.append(t(lang, "issue_healthy_ir", name=name, status=f" ({status})" if status else ""))
            elif in_taxi and _is_rookie(pro) is False:
                issues.append(t(lang, "issue_taxi", name=name))
    return issues


# ---------- Clasificación y resultados ----------

def _num(obj) -> float | None:
    """Fleaflicker da los números como {"value": 12.3, "formatted": "12.3"} o directamente."""
    if isinstance(obj, dict):
        obj = obj.get("value", obj.get("formatted"))
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _fmt(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:.2f}".rstrip("0").rstrip(".")


@dataclass
class StandingRow:
    rank: int
    team: Team
    wins: int
    losses: int
    ties: int
    points_for: float | None
    points_against: float | None
    division: str


async def fetch_standings(client: httpx.AsyncClient, league_id: int, sport: str | None) -> list[StandingRow]:
    params = {"league_id": league_id}
    if sport:
        params["sport"] = sport
    r = await client.get(f"{API}/FetchLeagueStandings", params=params, timeout=20)
    if r.status_code != 200:
        raise FleaflickerError(f"Fleaflicker respondió {r.status_code}")
    data = r.json()
    rows: list[StandingRow] = []
    for div in data.get("divisions", []):
        for raw in div.get("teams", []):
            if raw.get("id") is None:
                continue
            rec = raw.get("recordOverall") or raw.get("record") or {}
            rows.append(StandingRow(
                rank=int(rec.get("rank") or 0),
                team=_team(raw),
                wins=int(rec.get("wins") or 0),
                losses=int(rec.get("losses") or 0),
                ties=int(rec.get("ties") or 0),
                points_for=_num(raw.get("pointsFor")),
                points_against=_num(raw.get("pointsAgainst")),
                division=div.get("name") or "",
            ))
    if not rows:
        raise FleaflickerError("La liga no tiene clasificación")
    rows.sort(key=lambda r: (r.rank or 999, -(r.wins + r.ties / 2), -(r.points_for or 0)))
    return rows


@dataclass
class Game:
    away: Team
    home: Team
    away_score: float | None
    home_score: float | None
    final: bool


@dataclass
class Scoreboard:
    week: int | None
    games: list[Game]


async def fetch_scoreboard(client: httpx.AsyncClient, league_id: int, sport: str | None,
                           week: int | None = None) -> Scoreboard:
    params: dict = {"league_id": league_id}
    if sport:
        params["sport"] = sport
    if week:
        params["scoring_period"] = week
    r = await client.get(f"{API}/FetchLeagueScoreboard", params=params, timeout=20)
    if r.status_code != 200:
        raise FleaflickerError(f"Fleaflicker respondió {r.status_code}")
    data = r.json()
    period = data.get("schedulePeriod") or {}
    ordinal = period.get("ordinal") or period.get("value") or week
    games: list[Game] = []
    for g in data.get("games", []):
        away, home = g.get("away") or {}, g.get("home") or {}
        if away.get("id") is None or home.get("id") is None:
            continue
        games.append(Game(
            away=_team(away), home=_team(home),
            away_score=_num((g.get("awayScore") or {}).get("score", g.get("awayScore"))),
            home_score=_num((g.get("homeScore") or {}).get("score", g.get("homeScore"))),
            final=bool(g.get("isFinalScore")),
        ))
    if not games:
        raise FleaflickerError("No hay enfrentamientos esa jornada (¿la liga no es H2H?)")
    return Scoreboard(week=int(ordinal) if ordinal else None, games=games)
