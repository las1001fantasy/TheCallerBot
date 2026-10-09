"""Lectura del draft board de Fleaflicker (API pública, sin autenticación)."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

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
    """Prueba cada deporte hasta encontrar el draft board de la liga."""
    last: Exception | None = None
    for sport in SPORTS:
        try:
            await fetch_board(client, league_id, sport)
            return sport
        except (FleaflickerError, httpx.HTTPError, ValueError) as e:
            last = e
    raise FleaflickerError(f"No encuentro la liga {league_id} en Fleaflicker ({last})")


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
