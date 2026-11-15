"""NFL — fonte ESPN (mesma lógica do NBA, módulo separado)."""
import json
import os
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "nfl.db")
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
                id INTEGER PRIMARY KEY, date TEXT, ts INTEGER, season INTEGER, stype INTEGER, week INTEGER,
                home_id INTEGER, home_name TEXT, home_abbr TEXT, home_logo TEXT,
                away_id INTEGER, away_name TEXT, away_abbr TEXT, away_logo TEXT,
                home_pts INTEGER, away_pts INTEGER, status TEXT, status_detail TEXT,
                box_json TEXT, odds_json TEXT
            );
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


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _pair(s, sep="/"):
    try:
        a, b = str(s).split(sep)
        return float(a), float(b)
    except Exception:
        try:
            a, b = str(s).split("-")
            return float(a), float(b)
        except Exception:
            return 0.0, 0.0


# --------------------------------------------------------------- parse
def _row(e):
    c = e["competitions"][0]
    home = next(x for x in c["competitors"] if x["homeAway"] == "home")
    away = next(x for x in c["competitors"] if x["homeAway"] == "away")
    st = c["status"]["type"]
    status = "FT" if st.get("completed") else ("NS" if st.get("state") == "pre" else "LIVE")
    dt = datetime.strptime(e["date"][:17], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)

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
        return t.get("logo") or (t.get("logos") or [{}])[0].get("href") or f"https://a.espncdn.com/i/teamlogos/nfl/500/{t.get('abbreviation','').lower()}.png"

    return dict(
        id=int(e["id"]), date=dt.astimezone(LOCAL_TZ).isoformat(), ts=int(dt.timestamp()),
        season=(e.get("season") or {}).get("year"), stype=(e.get("season") or {}).get("type"), week=(e.get("week") or {}).get("number"),
        home_id=int(home["team"]["id"]), home_name=home["team"]["displayName"], home_abbr=home["team"].get("abbreviation"), home_logo=logo(home),
        away_id=int(away["team"]["id"]), away_name=away["team"]["displayName"], away_abbr=away["team"].get("abbreviation"), away_logo=logo(away),
        home_pts=pts(home), away_pts=pts(away), status=status, status_detail=st.get("shortDetail", ""),
    )


def upsert(row):
    with db() as con:
        con.execute(
            """INSERT INTO games(id,date,ts,season,stype,week,home_id,home_name,home_abbr,home_logo,away_id,away_name,away_abbr,away_logo,
                 home_pts,away_pts,status,status_detail)
               VALUES(:id,:date,:ts,:season,:stype,:week,:home_id,:home_name,:home_abbr,:home_logo,:away_id,:away_name,:away_abbr,:away_logo,
                 :home_pts,:away_pts,:status,:status_detail)
               ON CONFLICT(id) DO UPDATE SET date=excluded.date, ts=excluded.ts, home_pts=excluded.home_pts,
                 away_pts=excluded.away_pts, status=excluded.status, status_detail=excluded.status_detail""",
            row,
        )


# ------------------------------------------------------------ jogos da semana
def current_week():
    """Semana atual (jogos da rodada) direto do scoreboard."""
    key = "week:cur"
    c = cache_get(key, 600)
    if c is not None:
        return c
    d = get("scoreboard")
    rows = [_row(e) for e in d.get("events", [])]
    for r in rows:
        upsert(r)
    rows.sort(key=lambda r: r["ts"])
    out = dict(season=(d.get("season") or {}).get("year"), week=(d.get("week") or {}).get("number"), games=rows)
    cache_set(key, out)
    return out


def week_games(week=None, season=None, stype=2):
    if week is None:
        return current_week()
    key = f"week:{season}:{stype}:{week}"
    c = cache_get(key, 1800)
    if c is not None:
        return c
    params = dict(week=week, seasontype=stype)
    if season:
        params["dates"] = season
    d = get("scoreboard", **params)
    rows = [_row(e) for e in d.get("events", [])]
    for r in rows:
        upsert(r)
    rows.sort(key=lambda r: r["ts"])
    out = dict(season=(d.get("season") or {}).get("year"), week=week, games=rows)
    cache_set(key, out)
    return out


# ---------------------------------------------------------------- histórico
def _season_year():
    t = datetime.now(LOCAL_TZ)
    return t.year if t.month >= 8 else t.year - 1  # temporada 2026 vai de set/2026 a fev/2027


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


def team_games(team_id, n=10):
    yr = _season_year()
    order = [(yr, 3), (yr, 2), (yr - 1, 3), (yr - 1, 2)]
    fin = {}
    with ThreadPoolExecutor(4) as ex:
        for lst in ex.map(lambda a: _sched(team_id, *a), order):
            for r in lst:
                if r["status"] == "FT" and r["home_pts"] is not None:
                    fin[r["id"]] = r
    rows = sorted(fin.values(), key=lambda r: -r["ts"])[:n]
    for r in rows:
        upsert(r)
    return rows


TEAM_KEYS = ["firstDowns", "totalYards", "netPassingYards", "rushingYards", "rushingAttempts", "turnovers", "totalOffensivePlays",
             "possessionTime", "totalPenaltiesYards", "sacksYardsLost", "completionAttempts", "thirdDownEff", "redZoneAttempts"]


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
        tid = str(t["team"]["id"])
        d = box["teams"].setdefault(tid, {})
        for s in t.get("statistics", []):
            if s["name"] in TEAM_KEYS:
                d[s["name"]] = s.get("displayValue")
    # jogadores que atuaram (pra montar a lista)
    for p in data.get("boxscore", {}).get("players", []):
        tid = str(p["team"]["id"])
        lst = {}
        for s in p.get("statistics", []):
            if s["name"] not in ("passing", "rushing", "receiving", "defensive", "kicking"):
                continue
            for a in s.get("athletes", []):
                at = a["athlete"]
                lst.setdefault(str(at["id"]), dict(id=int(at["id"]), name=at["displayName"], cats=[]))["cats"].append(s["name"])
        box["players"][tid] = list(lst.values())
    with db() as con:
        con.execute("UPDATE games SET box_json=? WHERE id=?", (json.dumps(box), gid))
    return True


def game_odds(gid):
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


def team_history(team_id, n=10):
    rows = team_games(team_id, n)
    with ThreadPoolExecutor(5) as ex:
        list(ex.map(lambda r: fetch_box(r["id"]), rows))
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    with db() as con:
        out = con.execute(f"SELECT * FROM games WHERE id IN ({','.join('?' * len(ids))}) AND box_json IS NOT NULL ORDER BY ts DESC", ids).fetchall()
    return [dict(r) for r in out]


# ---------------------------------------------------------------- análise
TEAM_RULES = [
    ("Total de pontos (jogo)", [(f"Over {l}", (lambda l: lambda g: g["total"] > l)(l)) for l in (37.5, 41.5, 44.5, 47.5, 51.5)]),
    ("Pontos do time", [(f"Time over {l}", (lambda l: lambda g: g["pts_for"] > l)(l)) for l in (17.5, 20.5, 23.5, 27.5)]),
    ("1º tempo", [(f"Over {l} no 1ºT (jogo)", (lambda l: lambda g: g["h1_total"] > l)(l)) for l in (17.5, 20.5, 23.5, 27.5)]
                 + [(f"Time over {l} no 1ºT", (lambda l: lambda g: g["h1_for"] > l)(l)) for l in (6.5, 10.5, 13.5)]),
    ("Jardas do time", [(f"Over {l} jardas totais", (lambda l: lambda g: g["yards"] > l)(l)) for l in (299.5, 324.5, 349.5, 374.5)]
                      + [(f"Over {l} jardas de passe", (lambda l: lambda g: g["pass_yds"] > l)(l)) for l in (199.5, 224.5, 249.5)]
                      + [(f"Over {l} jardas corridas", (lambda l: lambda g: g["rush_yds"] > l)(l)) for l in (89.5, 109.5, 129.5)]),
    ("Outros", [("Time 1+ turnover", lambda g: g["turnovers"] >= 1), ("Time 2+ turnovers", lambda g: g["turnovers"] >= 2),
                ("Defesa 2+ sacks", lambda g: g["sacks_def"] >= 2), ("Defesa 3+ sacks", lambda g: g["sacks_def"] >= 3),
                ("Time 20+ first downs", lambda g: g["first_downs"] >= 20)]),
    ("Resultado", [("Venceu", lambda g: g["win"]), ("Venceu por 3+", lambda g: g["margin"] >= 3), ("Venceu por 7+", lambda g: g["margin"] >= 7),
                   ("Jogo decidido por ≤3", lambda g: abs(g["margin"]) <= 3), ("Jogo decidido por ≤7", lambda g: abs(g["margin"]) <= 7)]),
]


def game_metrics(row, team_id):
    box = json.loads(row["box_json"])
    home = row["home_id"] == team_id
    opp = row["away_id"] if home else row["home_id"]
    t, o = box["teams"].get(str(team_id), {}), box["teams"].get(str(opp), {})
    pf, pa = t.get("pts", 0), o.get("pts", 0)
    q, oq = t.get("q", []), o.get("q", [])
    h1 = sum(q[:2]); oh1 = sum(oq[:2])
    sacks_taken_by_opp = _pair(o.get("sacksYardsLost", "0-0"), "-")[0]  # sacks que o adversário SOFREU = sacks da nossa defesa
    return dict(
        game_id=row["id"], date=row["date"][:10], home=home, opponent=row["away_abbr"] if home else row["home_abbr"],
        opponent_name=row["away_name"] if home else row["home_name"], stype=SEASON_TYPES.get(row["stype"], ""), week=row.get("week"),
        score=f"{int(pf)}-{int(pa)}", pts_for=pf, pts_against=pa, total=pf + pa, win=pf > pa, margin=pf - pa,
        q1_for=q[0] if q else 0, q1_total=(q[0] if q else 0) + (oq[0] if oq else 0),
        h1_for=h1, h1_against=oh1, h1_total=h1 + oh1,
        yards=_num(t.get("totalYards")), pass_yds=_num(t.get("netPassingYards")), rush_yds=_num(t.get("rushingYards")),
        first_downs=_num(t.get("firstDowns")), turnovers=_num(t.get("turnovers")), sacks_def=sacks_taken_by_opp,
        yards_against=_num(o.get("totalYards")),
    )


# ------------------------------------------------- jogadores
# chave interna -> (nome ESPN no gamelog, rótulo)
GL_MAP = {
    "pass_yds": "passingYards", "pass_td": "passingTouchdowns", "pass_cmp": "completions", "pass_att": "passingAttempts", "int": "interceptions",
    "rush_att": "rushingAttempts", "rush_yds": "rushingYards", "rush_td": "rushingTouchdowns",
    "rec": "receptions", "tgt": "receivingTargets", "rec_yds": "receivingYards", "rec_td": "receivingTouchdowns",
    "tackles": "totalTackles", "solo": "soloTackles", "sacks": "sacks", "tfl": "tacklesForLoss", "pd": "passesDefended",
    "fgm": "fieldGoalsMade", "fga": "fieldGoalAttempts", "xpm": "extraPointsMade", "kpts": "totalKickingPoints",
}
PLAYER_STATS = [
    ("pass_yds", "Jardas de passe"), ("pass_td", "TD de passe"), ("pass_cmp", "Passes completos"), ("pass_att", "Passes tentados"), ("int", "Interceptações"),
    ("rush_yds", "Jardas corridas"), ("rush_att", "Corridas"), ("rush_td", "TD corrido"),
    ("rec", "Recepções"), ("rec_yds", "Jardas de recepção"), ("tgt", "Alvos"), ("rec_td", "TD de recepção"),
    ("rr_yds", "Jardas corrida+recepção"), ("any_td", "Touchdowns (corrida+recepção)"),
    ("tackles", "Tackles"), ("solo", "Tackles solo"), ("sacks", "Sacks"), ("tfl", "Tackles p/ perda"), ("pd", "Passes defendidos"),
    ("fgm", "Field goals"), ("fga", "FG tentados"), ("xpm", "Extra points"), ("kpts", "Pontos do kicker"),
]
# estatísticas mostradas por posição (as outras ficam escondidas)
POS_STATS = {
    "QB": ["pass_yds", "pass_td", "pass_cmp", "pass_att", "int", "rush_yds", "rush_att"],
    "RB": ["rush_yds", "rush_att", "rush_td", "rec", "rec_yds", "tgt", "rr_yds", "any_td"],
    "FB": ["rush_yds", "rush_att", "rec", "rec_yds", "rr_yds", "any_td"],
    "WR": ["rec", "rec_yds", "tgt", "rec_td", "rush_yds", "rr_yds", "any_td"],
    "TE": ["rec", "rec_yds", "tgt", "rec_td", "any_td"],
    "K": ["fgm", "fga", "xpm", "kpts"], "PK": ["fgm", "fga", "xpm", "kpts"],
}
DEF_STATS = ["tackles", "solo", "sacks", "tfl", "pd", "int"]


def _safe_line(values, min_hits):
    if len(values) < min_hits:
        return None
    v = sorted(values, reverse=True)
    thr = v[min_hits - 1]
    return int(thr) - 0.5 if thr >= 1 else None


def roster(team_id):
    key = f"roster:{team_id}"
    c = cache_get(key, 6 * 3600)
    if c is not None:
        return c
    out = {}
    try:
        d = get(f"teams/{team_id}/roster")
        for grp in d.get("athletes", []):
            for a in grp.get("items", []):
                out[str(a["id"])] = dict(id=int(a["id"]), name=a["displayName"], pos=(a.get("position") or {}).get("abbreviation", ""),
                                         jersey=a.get("jersey"), status=(a.get("status") or {}).get("name"), group=grp.get("position"),
                                         inj=[(i.get("status")) for i in a.get("injuries", [])])
    except Exception:
        pass
    cache_set(key, out)
    return out


def _parse_gamelog(d):
    names = d.get("names", [])
    events = d.get("events", {})
    rows = []
    for st in d.get("seasonTypes", []):
        sname = st.get("displayName", "")
        if "Preseason" in sname:
            continue
        for c in st.get("categories", []):
            for e in c.get("events") or []:
                ev = events.get(e["eventId"]) or {}
                v = dict(zip(names, e.get("stats", [])))
                r = dict(game_id=int(e["eventId"]), date=(ev.get("gameDate") or "")[:10], opp=(ev.get("opponent") or {}).get("abbreviation", ""),
                         home=ev.get("atVs") == "vs", week=ev.get("week"), season=sname[:4],
                         stype="Playoffs" if "Postseason" in sname else "Temporada regular")
                for k, nm in GL_MAP.items():
                    r[k] = _num(v.get(nm))
                r["rr_yds"] = r["rush_yds"] + r["rec_yds"]
                r["any_td"] = r["rush_td"] + r["rec_td"]
                rows.append(r)
    return rows


def player_log(pid, n=10):
    yr = _season_year()
    rows = []
    for season in (yr, yr - 1):
        key = f"plog:{pid}:{season}"
        c = cache_get(key, 4 * 3600 if season == yr else 30 * 86400)
        if c is None:
            try:
                r = requests.get(f"{WEB}/athletes/{pid}/gamelog", params={"season": season}, headers=HEADERS, timeout=25)
                c = _parse_gamelog(r.json()) if r.ok else []
            except Exception:
                c = []
            cache_set(key, c)
        rows.extend(c)
        if len(rows) >= n:
            break
    rows.sort(key=lambda r: (r["date"], r["game_id"]), reverse=True)
    return rows[:n]


def team_players(team_id, hist_rows, n=10):
    """Jogadores que atuaram nos últimos 3 jogos do time + gamelog de cada um."""
    ros = roster(team_id)
    seen = {}
    for row in hist_rows[:3]:
        box = json.loads(row["box_json"])
        for p in box["players"].get(str(team_id), []):
            seen.setdefault(str(p["id"]), p)
    players = []
    cur = str(_season_year())
    ids = list(seen.keys())
    with ThreadPoolExecutor(10) as ex:
        logs = list(ex.map(lambda pid: player_log(int(pid), n), ids))
    for pid, gl in zip(ids, logs):
        meta = ros.get(pid, {})
        pos = meta.get("pos") or ("QB" if "passing" in seen[pid]["cats"] else "K" if "kicking" in seen[pid]["cats"] else "DEF" if seen[pid]["cats"] == ["defensive"] else "")
        gp = len(gl)
        stats = {}
        for k, _ in PLAYER_STATS:
            vals = [g[k] for g in gl]
            if gp:
                stats[k] = dict(avg=round(sum(vals) / gp, 1), max=max(vals), min=min(vals), med=statistics.median(vals),
                                safe8=_safe_line(vals, max(1, round(gp * 0.8))), safe7=_safe_line(vals, max(1, round(gp * 0.7))))
            else:
                stats[k] = dict(avg=None, max=None, min=None, med=None, safe8=None, safe7=None)
        old = sum(1 for g in gl if g["season"] != cur)
        show = POS_STATS.get(pos) or (DEF_STATS if (pos in ("DE", "DT", "LB", "OLB", "ILB", "MLB", "CB", "S", "FS", "SS", "DB", "NT", "DL", "EDGE", "DEF") or "defensive" in seen[pid]["cats"]) else [])
        if not show:
            continue
        # relevância: soma das médias das stats "principais" pra ordenar
        key_stat = show[0]
        players.append(dict(id=int(pid), name=seen[pid]["name"], pos=pos, jersey=meta.get("jersey"), status=meta.get("status"),
                            inj=meta.get("inj", []), games=gp, stats=stats, old_games=old, show=show,
                            key_avg=stats[key_stat]["avg"] or 0, log=gl))
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FB": 4, "K": 5, "PK": 5}
    players.sort(key=lambda p: (order.get(p["pos"], 9), -p["key_avg"]))
    return players


def summarize_team(rows, team_id, n=10):
    games = [game_metrics(r, team_id) for r in rows][:n]
    N = len(games)
    avg = lambda k: round(sum(g[k] for g in games) / N, 1) if N else None
    averages = {k: dict(label=lbl, value=avg(k)) for k, lbl in [
        ("pts_for", "Pontos do time"), ("pts_against", "Pontos sofridos"), ("total", "Total do jogo"),
        ("h1_for", "Pontos 1ºT (time)"), ("h1_total", "Total 1ºT"), ("yards", "Jardas totais"), ("pass_yds", "Jardas de passe"),
        ("rush_yds", "Jardas corridas"), ("yards_against", "Jardas cedidas"), ("first_downs", "First downs"), ("turnovers", "Turnovers"),
        ("sacks_def", "Sacks da defesa"), ("margin", "Saldo médio")]}
    freqs = []
    for group, rules in TEAM_RULES:
        items = [dict(label=l, hits=sum(1 for g in games if fn(g)), n=N, pct=round(100 * sum(1 for g in games if fn(g)) / N) if N else None) for l, fn in rules]
        freqs.append(dict(group=group, items=items))
    players = team_players(team_id, rows, n)
    return dict(n=N, games=games, averages=averages, frequencies=freqs, players=players)


def all_markets():
    return [dict(group=g, items=[dict(id=f"{gi}_{i}", label=l) for i, (l, _) in enumerate(rules)]) for gi, (g, rules) in enumerate(TEAM_RULES)]
