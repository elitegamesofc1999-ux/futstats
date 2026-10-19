"""NBA — fonte ESPN (mesma lógica do futebol, módulo separado)."""
import json
import os
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "nba.db")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_HOURS", "-3"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FutStats/1.0"}
SEASON_TYPES = {1: "Pré-temporada", 2: "Temporada regular", 3: "Playoffs"}


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS games(
                id INTEGER PRIMARY KEY, date TEXT, ts INTEGER, season INTEGER, stype INTEGER,
                home_id INTEGER, home_name TEXT, home_abbr TEXT, home_logo TEXT,
                away_id INTEGER, away_name TEXT, away_abbr TEXT, away_logo TEXT,
                home_pts INTEGER, away_pts INTEGER, status TEXT, status_detail TEXT,
                box_json TEXT, odds_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_h ON games(home_id, ts);
            CREATE INDEX IF NOT EXISTS ix_a ON games(away_id, ts);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, saved_at INTEGER);
            """
        )


def cache_get(key, max_age):
    with db() as con:
        r = con.execute("SELECT value, saved_at FROM cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r["saved_at"] < max_age:
        return json.loads(r["value"])
    return None


def cache_set(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, json.dumps(value), int(time.time())))


def get(path, **params):
    r = requests.get(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------- parse
def _row(e):
    c = e["competitions"][0]
    home = next(x for x in c["competitors"] if x["homeAway"] == "home")
    away = next(x for x in c["competitors"] if x["homeAway"] == "away")
    st = c["status"]["type"]
    status = "FT" if st.get("completed") else ("NS" if st.get("state") == "pre" else "LIVE")
    dt = datetime.strptime(e["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)

    def pts(x):
        s = x.get("score")
        if isinstance(s, dict):
            s = s.get("value", s.get("displayValue"))
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None

    def logo(x):
        t = x["team"]
        return t.get("logo") or (t.get("logos") or [{}])[0].get("href") or f"https://a.espncdn.com/i/teamlogos/nba/500/{t.get('abbreviation','').lower()}.png"

    return dict(
        id=int(e["id"]), date=dt.astimezone(LOCAL_TZ).isoformat(), ts=int(dt.timestamp()),
        season=(e.get("season") or {}).get("year"), stype=(e.get("season") or {}).get("type"),
        home_id=int(home["team"]["id"]), home_name=home["team"]["displayName"], home_abbr=home["team"].get("abbreviation"), home_logo=logo(home),
        away_id=int(away["team"]["id"]), away_name=away["team"]["displayName"], away_abbr=away["team"].get("abbreviation"), away_logo=logo(away),
        home_pts=pts(home), away_pts=pts(away), status=status, status_detail=st.get("shortDetail", ""),
    )


def upsert(row):
    with db() as con:
        con.execute(
            """INSERT INTO games(id,date,ts,season,stype,home_id,home_name,home_abbr,home_logo,away_id,away_name,away_abbr,away_logo,
                 home_pts,away_pts,status,status_detail)
               VALUES(:id,:date,:ts,:season,:stype,:home_id,:home_name,:home_abbr,:home_logo,:away_id,:away_name,:away_abbr,:away_logo,
                 :home_pts,:away_pts,:status,:status_detail)
               ON CONFLICT(id) DO UPDATE SET date=excluded.date, ts=excluded.ts, home_pts=excluded.home_pts,
                 away_pts=excluded.away_pts, status=excluded.status, status_detail=excluded.status_detail""",
            row,
        )


# ------------------------------------------------------------ jogos do dia
def games_by_date(d):
    key = f"day:{d}"
    cached = cache_get(key, 600)
    if cached is not None:
        return cached
    day = datetime.strptime(d, "%Y-%m-%d").date()
    rows, seen = [], set()

    def one(dt):
        try:
            return [_row(e) for e in get("scoreboard", dates=dt).get("events", [])]
        except Exception:
            return []

    with ThreadPoolExecutor(2) as ex:
        for lst in ex.map(one, [f"{(day - timedelta(days=1)):%Y%m%d}", f"{day:%Y%m%d}"]):
            for r in lst:
                if r["id"] not in seen and r["date"][:10] == d:
                    seen.add(r["id"]); rows.append(r)
    for r in rows:
        upsert(r)
    rows.sort(key=lambda r: r["ts"])
    cache_set(key, rows)
    return rows


# ---------------------------------------------------------------- histórico
def _season_year():
    t = datetime.now(LOCAL_TZ)
    return t.year + 1 if t.month >= 9 else t.year  # ESPN: temporada 2026-27 = 2027


def _sched(team_id, season, stype):
    key = f"sched:{team_id}:{season}:{stype}"
    cached = cache_get(key, 4 * 3600 if season == _season_year() else 30 * 86400)
    if cached is None:
        try:
            cached = [dict(_row(e), season=season, stype=stype) for e in get(f"teams/{team_id}/schedule", season=season, seasontype=stype).get("events", [])]
        except Exception:
            cached = []
        cache_set(key, cached)
    return cached


def team_games(team_id, n=10, include_pre=False):
    """Últimos N jogos finalizados: regular+playoffs da temporada atual; se faltar, temporada anterior."""
    yr = _season_year()
    order = [(yr, 3), (yr, 2)] + ([(yr, 1)] if include_pre else []) + [(yr - 1, 3), (yr - 1, 2)]
    fin = {}
    with ThreadPoolExecutor(5) as ex:
        for lst in ex.map(lambda a: _sched(team_id, *a), order):
            for r in lst:
                if r["status"] == "FT" and r["home_pts"] is not None:
                    fin[r["id"]] = r
    rows = sorted(fin.values(), key=lambda r: -r["ts"])[:n]
    for r in rows:
        upsert(r)
    return rows


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _made_att(s):
    try:
        m, a = s.split("-")
        return float(m), float(a)
    except Exception:
        return 0.0, 0.0


def fetch_box(gid):
    with db() as con:
        r = con.execute("SELECT box_json FROM games WHERE id=?", (gid,)).fetchone()
    if r and r["box_json"]:
        return True
    try:
        data = get("summary", event=gid)
    except Exception:
        return False
    box = {"teams": {}, "players": {}}
    try:
        for c in data["header"]["competitions"][0]["competitors"]:
            q = [_num(l.get("displayValue")) for l in c.get("linescores", [])]
            box["teams"][str(c["team"]["id"])] = {"pts": _num(c.get("score")), "q": q}
    except (KeyError, IndexError):
        return False
    for t in data.get("boxscore", {}).get("teams", []):
        d = box["teams"].setdefault(str(t["team"]["id"]), {})
        for s in t.get("statistics", []):
            if s["name"] == "threePointFieldGoalsMade-threePointFieldGoalsAttempted":
                d["tpm"], d["tpa"] = _made_att(s["displayValue"])
            elif s["name"] == "fieldGoalsMade-fieldGoalsAttempted":
                d["fgm"], d["fga"] = _made_att(s["displayValue"])
            elif s["name"] in ("totalRebounds", "assists", "steals", "blocks", "turnovers", "freeThrowsMade-freeThrowsAttempted"):
                d[s["name"]] = _num(s["displayValue"].split("-")[0]) if "-" in s["displayValue"] else _num(s["displayValue"])
    for t in data.get("boxscore", {}).get("players", []):
        lst = []
        for st in t.get("statistics", []):
            names = st.get("names", [])
            for a in st.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                vals = dict(zip(names, a["stats"]))
                mins = _num(vals.get("MIN"))
                if mins <= 0:
                    continue
                tpm, tpa = _made_att(vals.get("3PT", "0-0"))
                fgm, fga = _made_att(vals.get("FG", "0-0"))
                ftm, fta = _made_att(vals.get("FT", "0-0"))
                lst.append(dict(
                    id=int(a["athlete"]["id"]), name=a["athlete"]["displayName"], short=a["athlete"].get("shortName"),
                    pos=(a["athlete"].get("position") or {}).get("abbreviation", ""), starter=bool(a.get("starter")),
                    min=mins, pts=_num(vals.get("PTS")), reb=_num(vals.get("REB")), ast=_num(vals.get("AST")),
                    tpm=tpm, tpa=tpa, fgm=fgm, fga=fga, ftm=ftm, fta=fta,
                    stl=_num(vals.get("STL")), blk=_num(vals.get("BLK")), to=_num(vals.get("TO")),
                ))
        box["players"][str(t["team"]["id"])] = lst
    odds = None
    pc = data.get("pickcenter") or []
    if pc:
        o = pc[0]
        odds = dict(provider=(o.get("provider") or {}).get("name"), total=o.get("overUnder"), spread=o.get("spread"),
                    details=o.get("details"))
    with db() as con:
        con.execute("UPDATE games SET box_json=?, odds_json=? WHERE id=?", (json.dumps(box), json.dumps(odds), gid))
    return True


def game_odds(gid):
    """Linha (total/spread) de um jogo ainda não iniciado — cache 30 min."""
    key = f"odds:{gid}"
    c = cache_get(key, 1800)
    if c is not None:
        return c
    try:
        pc = get("summary", event=gid).get("pickcenter") or []
    except Exception:
        pc = []
    odds = None
    if pc:
        o = pc[0]
        odds = dict(provider=(o.get("provider") or {}).get("name"), total=o.get("overUnder"), spread=o.get("spread"), details=o.get("details"))
    cache_set(key, odds)
    return odds


def team_history(team_id, n=10, include_pre=False):
    rows = team_games(team_id, n, include_pre)
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda r: fetch_box(r["id"]), rows))
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    with db() as con:
        out = con.execute(f"SELECT * FROM games WHERE id IN ({','.join('?' * len(ids))}) AND box_json IS NOT NULL ORDER BY ts DESC", ids).fetchall()
    return [dict(r) for r in out]


# ---------------------------------------------------------------- análise
TEAM_RULES = [
    ("Total de pontos (jogo)", [(f"Over {l}", (lambda l: lambda g: g["total"] > l)(l)) for l in (210.5, 215.5, 220.5, 225.5, 230.5, 235.5, 240.5)]),
    ("Pontos do time", [(f"Time over {l}", (lambda l: lambda g: g["pts_for"] > l)(l)) for l in (105.5, 110.5, 115.5, 120.5, 125.5)]),
    ("1º tempo", [(f"Over {l} no 1ºT (jogo)", (lambda l: lambda g: g["h1_total"] > l)(l)) for l in (105.5, 110.5, 115.5, 120.5)]
                 + [(f"Time over {l} no 1ºT", (lambda l: lambda g: g["h1_for"] > l)(l)) for l in (52.5, 57.5, 62.5)]),
    ("1º quarto", [(f"Over {l} no 1ºQ (jogo)", (lambda l: lambda g: g["q1_total"] > l)(l)) for l in (52.5, 55.5, 58.5, 61.5)]),
    ("Bolas de 3", [(f"Time {int(l + .5)}+ bolas de 3", (lambda l: lambda g: g["tpm_for"] > l)(l)) for l in (10.5, 12.5, 14.5, 16.5, 18.5)]
                   + [(f"Over {l} bolas de 3 (jogo)", (lambda l: lambda g: g["tpm_total"] > l)(l)) for l in (22.5, 25.5, 28.5)]),
    ("Resultado", [("Venceu", lambda g: g["win"]), ("Venceu por 5+", lambda g: g["margin"] >= 5), ("Venceu por 10+", lambda g: g["margin"] >= 10),
                   ("Jogo decidido por ≤5", lambda g: abs(g["margin"]) <= 5)]),
]


def game_metrics(row, team_id):
    box = json.loads(row["box_json"])
    home = row["home_id"] == team_id
    opp = row["away_id"] if home else row["home_id"]
    t, o = box["teams"].get(str(team_id), {}), box["teams"].get(str(opp), {})
    pf, pa = t.get("pts", 0), o.get("pts", 0)
    q, oq = t.get("q", []), o.get("q", [])
    h1 = sum(q[:2]); oh1 = sum(oq[:2])
    return dict(
        game_id=row["id"], date=row["date"][:10], home=home, opponent=row["away_abbr"] if home else row["home_abbr"],
        opponent_name=row["away_name"] if home else row["home_name"], stype=SEASON_TYPES.get(row["stype"], ""),
        score=f"{int(pf)}-{int(pa)}", pts_for=pf, pts_against=pa, total=pf + pa, win=pf > pa, margin=pf - pa,
        q1_for=q[0] if q else 0, q1_total=(q[0] if q else 0) + (oq[0] if oq else 0),
        h1_for=h1, h1_against=oh1, h1_total=h1 + oh1,
        tpm_for=t.get("tpm", 0), tpm_against=o.get("tpm", 0), tpm_total=t.get("tpm", 0) + o.get("tpm", 0),
        reb_for=t.get("totalRebounds", 0), ast_for=t.get("assists", 0),
    )


PLAYER_STATS = [("pts", "Pontos"), ("reb", "Rebotes"), ("ast", "Assistências"), ("tpm", "Bolas de 3"),
                ("pra", "Pts+Reb+Ast"), ("stl", "Roubos"), ("blk", "Tocos"), ("to", "Turnovers")]


def _safe_line(values, min_hits):
    """Maior linha X.5 que o jogador superou em pelo menos `min_hits` dos jogos."""
    if len(values) < min_hits:
        return None
    v = sorted(values, reverse=True)
    thr = v[min_hits - 1]  # min_hits-ésimo maior valor
    line = int(thr) - 0.5 if thr >= 1 else None
    return line


def summarize_team(rows, team_id, n=10):
    games = [game_metrics(r, team_id) for r in rows][:n]
    N = len(games)
    avg = lambda k: round(sum(g[k] for g in games) / N, 1) if N else None
    averages = {k: dict(label=lbl, value=avg(k)) for k, lbl in [
        ("pts_for", "Pontos do time"), ("pts_against", "Pontos sofridos"), ("total", "Total do jogo"),
        ("h1_for", "Pontos 1ºT (time)"), ("h1_total", "Total 1ºT"), ("q1_total", "Total 1ºQ"),
        ("tpm_for", "Bolas de 3 (time)"), ("tpm_total", "Bolas de 3 (jogo)"), ("reb_for", "Rebotes"), ("ast_for", "Assistências"), ("margin", "Saldo médio")]}
    freqs = []
    for group, rules in TEAM_RULES:
        items = [dict(label=l, hits=sum(1 for g in games if fn(g)), n=N, pct=round(100 * sum(1 for g in games if fn(g)) / N) if N else None) for l, fn in rules]
        freqs.append(dict(group=group, items=items))
    # jogadores
    logs = {}
    for r in rows[:n]:
        box = json.loads(r["box_json"])
        for p in box["players"].get(str(team_id), []):
            p = dict(p, pra=p["pts"] + p["reb"] + p["ast"], date=r["date"][:10],
                     opp=(r["away_abbr"] if r["home_id"] == team_id else r["home_abbr"]))
            logs.setdefault((p["id"], p["name"], p["pos"]), []).append(p)
    players = []
    for (pid, name, pos), gl in logs.items():
        gp = len(gl)
        stats = {}
        for k, lbl in PLAYER_STATS:
            vals = [g[k] for g in gl]
            stats[k] = dict(avg=round(sum(vals) / gp, 1), max=max(vals), min=min(vals), med=statistics.median(vals),
                            last=vals[:5], safe8=_safe_line(vals, max(1, round(gp * 0.8))), safe7=_safe_line(vals, max(1, round(gp * 0.7))))
        players.append(dict(id=pid, name=name, pos=pos, games=gp, min=round(sum(g["min"] for g in gl) / gp, 1),
                            starter=sum(1 for g in gl if g["starter"]), stats=stats,
                            log=[dict(date=g["date"], opp=g["opp"], min=g["min"], pts=g["pts"], reb=g["reb"], ast=g["ast"], tpm=g["tpm"], stl=g["stl"], blk=g["blk"], to=g["to"], pra=g["pra"]) for g in gl]))
    players.sort(key=lambda p: (-p["min"] * min(p["games"], 5), -p["stats"]["pts"]["avg"]))
    return dict(n=N, games=games, averages=averages, frequencies=freqs, players=players[:12])


def all_markets():
    return [dict(group=g, items=[dict(id=f"{gi}_{i}", label=l) for i, (l, _) in enumerate(rules)]) for gi, (g, rules) in enumerate(TEAM_RULES)]
