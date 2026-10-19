import json
import os
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import espn as E
from analysis import summarize, all_markets
from concurrent.futures import ThreadPoolExecutor
import time

_screener_cache = {}

app = FastAPI(title="FutStats")
STATIC = os.path.join(os.path.dirname(__file__), "static")
HISTORY_N = int(os.environ.get("HISTORY_N", "10"))


@app.on_event("startup")
def _startup():
    E.init_db()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/status")
def status():
    with E.db() as con:
        total = con.execute("SELECT COUNT(*) c FROM fixtures WHERE stats_json IS NOT NULL").fetchone()["c"]
    return dict(source="ESPN", games_with_stats=total,
                leagues=[dict(id=k, name=v[0], flag=v[1], group=v[2], default=v[3]) for k, v in E.LEAGUES.items()])


@app.get("/api/today")
def today(d: str | None = None):
    d = d or date.today().isoformat()
    rows = E.fixtures_by_date(d)
    for r in rows:
        r["league_flag"] = E.LEAGUES.get(r["league_id"], ("", ""))[1]
    return dict(date=d, fixtures=rows)


@app.get("/api/match/{fid}")
def match(fid: int, venue: int = 0):
    with E.db() as con:
        f = con.execute("SELECT * FROM fixtures WHERE id=?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404, "jogo não encontrado — abra a lista do dia primeiro")
    f = dict(f)
    k = HISTORY_N * 2 if venue else HISTORY_N
    home_hist = E.team_history(f["home_id"], f["league_id"], k)
    away_hist = E.team_history(f["away_id"], f["league_id"], k)
    return dict(
        fixture={k: f[k] for k in f if k not in ("stats_json", "players_json")}, venue=bool(venue),
        home=summarize(home_hist, f["home_id"], "home" if venue else None, HISTORY_N),
        away=summarize(away_hist, f["away_id"], "away" if venue else None, HISTORY_N),
        h2h=E.h2h(f["home_id"], f["away_id"]), history_n=HISTORY_N, notes=[],
    )


@app.get("/api/markets")
def markets():
    return all_markets()


def _fixture_markets(f, venue=0):
    k = HISTORY_N * 2 if venue else HISTORY_N
    H = summarize(E.team_history(f["home_id"], f["league_id"], k), f["home_id"], "home" if venue else None, HISTORY_N)
    A = summarize(E.team_history(f["away_id"], f["league_id"], k), f["away_id"], "away" if venue else None, HISTORY_N)
    out = {}
    for gh, ga in zip(H["frequencies"], A["frequencies"]):
        for ih, ia in zip(gh["items"], ga["items"]):
            tot_n = ih["n"] + ia["n"]
            out[ih["id"]] = dict(home_hits=ih["hits"], home_n=ih["n"], away_hits=ia["hits"], away_n=ia["n"],
                                 pct=round(100 * (ih["hits"] + ia["hits"]) / tot_n) if tot_n else None)
    return dict(id=f["id"], date=f["date"], league_name=f["league_name"], league_id=f["league_id"],
                league_flag=E.LEAGUES.get(f["league_id"], ("", ""))[1], status=f["status"],
                home_name=f["home_name"], home_logo=f["home_logo"], away_name=f["away_name"], away_logo=f["away_logo"],
                home_goals=f["home_goals"], away_goals=f["away_goals"],
                avg=dict(goals=(H["averages"]["goals"]["value"], A["averages"]["goals"]["value"]),
                         corners=(H["averages"]["corners"]["value"], A["averages"]["corners"]["value"]),
                         cards=(H["averages"]["cards"]["value"], A["averages"]["cards"]["value"])),
                markets=out)


@app.get("/api/screener")
def screener(d: str | None = None, venue: int = 0, leagues: str | None = None):
    """Para TODOS os jogos do dia, calcula quantas vezes cada mercado bateu nos últimos 10 de cada time.
    venue=1 -> mandante só jogos em casa, visitante só jogos fora."""
    d = d or date.today().isoformat()
    sel = set(leagues.split(",")) if leagues else set(E.DEFAULT_LEAGUES)
    ck = (d, venue, tuple(sorted(sel)))
    c = _screener_cache.get(ck)
    if c and time.time() - c[0] < 900:
        return c[1]
    rows = [r for r in E.fixtures_by_date(d) if r["league_id"] in sel]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda f: _fixture_markets(f, venue), rows))
    out = dict(date=d, venue=bool(venue), leagues=sorted(sel), markets=all_markets(), fixtures=res)
    _screener_cache[ck] = (time.time(), out)
    return out


# ======================================================================
#  NBA  (módulo separado — não altera nada do futebol)
# ======================================================================
import nba as N  # noqa: E402

_nba_screener_cache = {}


@app.on_event("startup")
def _startup_nba():
    N.init_db()


@app.get("/nba")
def nba_index():
    return FileResponse(os.path.join(STATIC, "nba.html"))


@app.get("/api/nba/today")
def nba_today(d: str | None = None):
    d = d or date.today().isoformat()
    rows = N.games_by_date(d)
    return dict(date=d, games=rows)


@app.get("/api/nba/markets")
def nba_markets():
    return N.all_markets()


@app.get("/api/nba/game/{gid}")
def nba_game(gid: int, pre: int = 0):
    with N.db() as con:
        g = con.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not g:
        raise HTTPException(404, "jogo não encontrado — abra a lista do dia primeiro")
    g = dict(g)
    with ThreadPoolExecutor(2) as ex:
        fh, fa = ex.submit(N.team_history, g["home_id"], HISTORY_N, bool(pre)), ex.submit(N.team_history, g["away_id"], HISTORY_N, bool(pre))
        home_hist, away_hist = fh.result(), fa.result()
    odds = N.game_odds(gid) if g["status"] != "FT" else (json.loads(g["odds_json"]) if g.get("odds_json") else None)
    return dict(game={k: g[k] for k in g if k not in ("box_json", "odds_json")}, odds=odds, history_n=HISTORY_N,
                home=N.summarize_team(home_hist, g["home_id"], HISTORY_N), away=N.summarize_team(away_hist, g["away_id"], HISTORY_N))


def _nba_game_markets(g):
    H = N.summarize_team(N.team_history(g["home_id"], HISTORY_N), g["home_id"], HISTORY_N)
    A = N.summarize_team(N.team_history(g["away_id"], HISTORY_N), g["away_id"], HISTORY_N)
    out = {}
    for gi, (gh, ga) in enumerate(zip(H["frequencies"], A["frequencies"])):
        for i, (ih, ia) in enumerate(zip(gh["items"], ga["items"])):
            tn = ih["n"] + ia["n"]
            out[f"{gi}_{i}"] = dict(home_hits=ih["hits"], home_n=ih["n"], away_hits=ia["hits"], away_n=ia["n"], pct=round(100 * (ih["hits"] + ia["hits"]) / tn) if tn else None)
    return dict(**{k: g[k] for k in g if k not in ("box_json", "odds_json")}, markets=out,
                avg=dict(total=(H["averages"]["total"]["value"], A["averages"]["total"]["value"]), pts=(H["averages"]["pts_for"]["value"], A["averages"]["pts_for"]["value"])))


@app.get("/api/nba/screener")
def nba_screener(d: str | None = None):
    d = d or date.today().isoformat()
    c = _nba_screener_cache.get(d)
    if c and time.time() - c[0] < 900:
        return c[1]
    rows = N.games_by_date(d)
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(_nba_game_markets, rows))
    out = dict(date=d, markets=N.all_markets(), games=res)
    _nba_screener_cache[d] = (time.time(), out)
    return out
