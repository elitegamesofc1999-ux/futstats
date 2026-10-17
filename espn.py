"""
Fonte de dados: API pública da ESPN (sem chave, sem limite de datas).
Salva tudo em SQLite para o site ficar rápido.
"""
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "espn.db")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_HOURS", "-3"))  # Fortaleza = UTC-3
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

LEAGUES = {
    "eng.1": ("Premier League", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "esp.1": ("La Liga", "🇪🇸"),
    "ita.1": ("Serie A", "🇮🇹"),
    "ger.1": ("Bundesliga", "🇩🇪"),
    "fra.1": ("Ligue 1", "🇫🇷"),
    "uefa.champions": ("Champions League", "🏆"),
    "uefa.europa": ("Europa League", "🏆"),
    "bra.1": ("Brasileirão", "🇧🇷"),
}
# competições extras consultadas para cada time (além da liga dele)
EXTRA_COMPETITIONS = {
    "eng.1": ["uefa.champions", "uefa.europa", "uefa.europa.conf", "eng.fa", "eng.league_cup"],
    "esp.1": ["uefa.champions", "uefa.europa", "uefa.europa.conf", "esp.copa_del_rey"],
    "ita.1": ["uefa.champions", "uefa.europa", "uefa.europa.conf", "ita.coppa_italia"],
    "ger.1": ["uefa.champions", "uefa.europa", "uefa.europa.conf", "ger.dfb_pokal"],
    "fra.1": ["uefa.champions", "uefa.europa", "uefa.europa.conf", "fra.coupe_de_france"],
    "uefa.champions": ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "por.1", "ned.1"],
    "uefa.europa": ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "por.1", "ned.1"],
    "bra.1": ["bra.copa_do_brasil", "conmebol.libertadores", "conmebol.sudamericana"],
}
CUP_NAMES = {
    "uefa.europa.conf": "Conference League", "eng.fa": "FA Cup", "eng.league_cup": "Copa da Liga",
    "esp.copa_del_rey": "Copa del Rey", "ita.coppa_italia": "Coppa Italia", "ger.dfb_pokal": "DFB Pokal",
    "fra.coupe_de_france": "Coupe de France", "bra.copa_do_brasil": "Copa do Brasil",
    "conmebol.libertadores": "Libertadores", "conmebol.sudamericana": "Sul-Americana",
    "por.1": "Liga Portugal", "ned.1": "Eredivisie",
}

STAT_MAP = {  # nome ESPN -> nome usado em analysis.py
    "wonCorners": "Corner Kicks", "yellowCards": "Yellow Cards", "redCards": "Red Cards",
    "totalShots": "Total Shots", "shotsOnTarget": "Shots on Goal", "foulsCommitted": "Fouls",
    "offsides": "Offsides", "possessionPct": "Ball Possession", "saves": "Goalkeeper Saves",
    "blockedShots": "Blocked Shots", "totalTackles": "Tackles",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FutStats/1.0"}


# ----------------------------------------------------------------- DB
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS fixtures(
                id INTEGER PRIMARY KEY,
                date TEXT, ts INTEGER, league_id TEXT, league_name TEXT,
                home_id INTEGER, home_name TEXT, home_logo TEXT,
                away_id INTEGER, away_name TEXT, away_logo TEXT,
                home_goals INTEGER, away_goals INTEGER, status TEXT,
                stats_json TEXT, players_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_home ON fixtures(home_id, ts);
            CREATE INDEX IF NOT EXISTS ix_away ON fixtures(away_id, ts);
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


# ------------------------------------------------------------ parsing
def _event_row(e, league_id):
    c = e["competitions"][0]
    home = next(x for x in c["competitors"] if x["homeAway"] == "home")
    away = next(x for x in c["competitors"] if x["homeAway"] == "away")
    st = c["status"]["type"]
    status = "FT" if st.get("completed") else ("NS" if st.get("state") == "pre" else "LIVE")
    dt = datetime.strptime(e["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    local = dt.astimezone(LOCAL_TZ)

    def score(x):
        s = x.get("score")
        if isinstance(s, dict):
            s = s.get("value", s.get("displayValue"))
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None

    def logo(x):
        t = x["team"]
        if t.get("logo"):
            return t["logo"]
        if t.get("logos"):
            return t["logos"][0]["href"]
        return f"https://a.espncdn.com/i/teamlogos/soccer/500/{t['id']}.png"

    return dict(
        id=int(e["id"]), date=local.isoformat(), ts=int(dt.timestamp()),
        league_id=league_id, league_name=LEAGUES.get(league_id, (CUP_NAMES.get(league_id, league_id),))[0],
        home_id=int(home["team"]["id"]), home_name=home["team"]["displayName"], home_logo=logo(home),
        away_id=int(away["team"]["id"]), away_name=away["team"]["displayName"], away_logo=logo(away),
        home_goals=score(home), away_goals=score(away), status=status,
        status_detail=st.get("shortDetail", ""),
    )


def upsert_fixture(row):
    with db() as con:
        con.execute(
            """INSERT INTO fixtures(id,date,ts,league_id,league_name,home_id,home_name,home_logo,
                 away_id,away_name,away_logo,home_goals,away_goals,status)
               VALUES(:id,:date,:ts,:league_id,:league_name,:home_id,:home_name,:home_logo,
                 :away_id,:away_name,:away_logo,:home_goals,:away_goals,:status)
               ON CONFLICT(id) DO UPDATE SET date=excluded.date, ts=excluded.ts,
                 home_goals=excluded.home_goals, away_goals=excluded.away_goals, status=excluded.status""",
            row,
        )


# ------------------------------------------------------ jogos do dia
def fixtures_by_date(d: str, leagues=None):
    """d = 'YYYY-MM-DD' (horário local). Consulta o dia e o seguinte em UTC e filtra."""
    leagues = leagues or list(LEAGUES)
    key = f"day:{d}:{','.join(leagues)}"
    cached = cache_get(key, 600)
    if cached is not None:
        return cached
    day = datetime.strptime(d, "%Y-%m-%d").date()
    dates = [f"{day:%Y%m%d}", f"{(day + timedelta(days=1)):%Y%m%d}"]

    def one(arg):
        lg, dt = arg
        try:
            data = get(f"{lg}/scoreboard", dates=dt)
        except Exception:
            return []
        return [_event_row(e, lg) for e in data.get("events", [])]

    rows, seen = [], set()
    with ThreadPoolExecutor(8) as ex:
        for lst in ex.map(one, [(lg, dt) for lg in leagues for dt in dates]):
            for r in lst:
                if r["id"] not in seen:
                    seen.add(r["id"]); rows.append(r)
    rows = [r for r in rows if r["date"][:10] == d]
    for r in rows:
        upsert_fixture(r)
    rows.sort(key=lambda r: r["ts"])
    cache_set(key, rows)
    return rows


# ------------------------------------------------- histórico do time
def _season_year(league_id):
    today = datetime.now(LOCAL_TZ)
    if league_id.startswith(("bra", "conmebol")):
        return today.year
    return today.year if today.month >= 7 else today.year - 1


def _sched(team_id, lg, season=None):
    key = f"sched:{lg}:{team_id}:{season or 'cur'}"
    cached = cache_get(key, 6 * 3600 if season is None else 30 * 86400)
    if cached is None:
        try:
            params = {"season": season} if season else {}
            data = get(f"{lg}/teams/{team_id}/schedule", **params)
            cached = [_event_row(e, lg) for e in data.get("events", [])]
        except Exception:
            cached = []
        cache_set(key, cached)
    return cached


def team_schedule(team_id, league_id, n=10):
    """Últimos jogos finalizados do time: liga + copas/continentais; se faltar, temporada anterior."""
    comps = [league_id] + [c for c in EXTRA_COMPETITIONS.get(league_id, []) if c != league_id]
    events = {}
    with ThreadPoolExecutor(6) as ex:
        for lst in ex.map(lambda lg: _sched(team_id, lg), comps):
            for r in lst:
                events[r["id"]] = r
    fin = [r for r in events.values() if r["status"] == "FT" and r["home_goals"] is not None]
    if len(fin) < n:  # início de temporada: completa com a anterior
        prev = _season_year(league_id) - 1
        with ThreadPoolExecutor(6) as ex:
            for lst in ex.map(lambda lg: _sched(team_id, lg, prev), comps):
                for r in lst:
                    if r["id"] not in events and r["status"] == "FT" and r["home_goals"] is not None:
                        events[r["id"]] = r; fin.append(r)
    fin.sort(key=lambda r: -r["ts"])
    return fin


def fetch_summary(fid, league_id):
    """Estatísticas + jogadores de um jogo finalizado (salvo para sempre)."""
    with db() as con:
        r = con.execute("SELECT stats_json FROM fixtures WHERE id=?", (fid,)).fetchone()
    if r and r["stats_json"]:
        return True
    try:
        data = get(f"{league_id}/summary", event=fid)
    except Exception:
        return False
    stats, players = {}, {}
    for t in data.get("boxscore", {}).get("teams", []):
        d = {}
        for s in t.get("statistics", []):
            name = STAT_MAP.get(s["name"])
            if name:
                try:
                    d[name] = float(s["displayValue"])
                except ValueError:
                    d[name] = 0
        stats[str(t["team"]["id"])] = d
    for t in data.get("rosters", []):
        lst = []
        for p in t.get("roster", []):
            st = {s["name"]: s.get("value") or 0 for s in p.get("stats", [])}
            if not st.get("appearances") and not p.get("starter"):
                continue
            a = p["athlete"]
            lst.append(dict(
                id=int(a["id"]), name=a["displayName"], photo=a.get("headshot", {}).get("href") if isinstance(a.get("headshot"), dict) else None,
                minutes=0, shots=st.get("totalShots", 0), shots_on=st.get("shotsOnTarget", 0),
                goals=st.get("totalGoals", 0), assists=st.get("goalAssists", 0),
                fouls=st.get("foulsCommitted", 0), fouled=st.get("foulsSuffered", 0),
                yellow=st.get("yellowCards", 0), red=st.get("redCards", 0), tackles=0, key_passes=0,
            ))
        players[str(t["team"]["id"])] = lst
    # 1º tempo: placar do intervalo + cartões por período
    try:
        for x in data["header"]["competitions"][0]["competitors"]:
            ls = x.get("linescores") or []
            if ls:
                stats.setdefault(str(x["team"]["id"]), {})["HT Goals"] = float(ls[0].get("displayValue") or 0)
    except (KeyError, IndexError, ValueError):
        pass
    for k in data.get("keyEvents", []):
        tid = str((k.get("team") or {}).get("id", ""))
        if tid not in stats or (k.get("period") or {}).get("number") != 1:
            continue
        txt = (k.get("type") or {}).get("text", "")
        if "Card" in txt:
            stats[tid]["HT Cards"] = stats[tid].get("HT Cards", 0) + 1
    for tid in stats:
        stats[tid].setdefault("HT Cards", 0)
    if not stats:
        return False
    with db() as con:
        con.execute("UPDATE fixtures SET stats_json=?, players_json=? WHERE id=?",
                    (json.dumps(stats), json.dumps(players), fid))
    return True


def team_history(team_id, league_id, n=10):
    sched = team_schedule(team_id, league_id, n)[:n]
    for r in sched:
        upsert_fixture(r)
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda r: fetch_summary(r["id"], r["league_id"]), sched))
    ids = [r["id"] for r in sched]
    if not ids:
        return []
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM fixtures WHERE id IN ({','.join('?' * len(ids))}) AND stats_json IS NOT NULL ORDER BY ts DESC",
            ids,
        ).fetchall()
    return [dict(r) for r in rows]


def h2h(home_id, away_id, limit=5):
    with db() as con:
        return [dict(r) for r in con.execute(
            """SELECT id,date,league_name,home_name,away_name,home_goals,away_goals FROM fixtures
               WHERE status='FT' AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
               ORDER BY ts DESC LIMIT ?""", (home_id, away_id, away_id, home_id, limit)).fetchall()]
