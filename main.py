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


_an_cache = {}


def _analyze(f, venue=0):
    """Resumo (H, A) de um jogo, cacheado 15 min."""
    ck = (f["id"], venue)
    c = _an_cache.get(ck)
    if c and time.time() - c[0] < 900:
        return c[1]
    k = HISTORY_N * 2 if venue else HISTORY_N
    H = summarize(E.team_history(f["home_id"], f["league_id"], k), f["home_id"], "home" if venue else None, HISTORY_N)
    A = summarize(E.team_history(f["away_id"], f["league_id"], k), f["away_id"], "away" if venue else None, HISTORY_N)
    _an_cache[ck] = (time.time(), (H, A))
    return H, A


def _fixture_markets(f, venue=0):
    H, A = _analyze(f, venue)
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
                home=N.summarize_team(home_hist, g["home_id"], HISTORY_N, bool(pre)), away=N.summarize_team(away_hist, g["away_id"], HISTORY_N, bool(pre)))


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


# ---------------- escalação provável (RotoWire + lesões ESPN) ----------------
import rotowire as RW  # noqa: E402


@app.get("/api/nba/lineup/{gid}")
def nba_lineup(gid: int):
    with N.db() as con:
        g = con.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not g:
        raise HTTPException(404, "jogo não encontrado")
    return RW.lineups_for(g["away_abbr"], g["home_abbr"], g["away_id"], g["home_id"])


@app.get("/api/nba/lineups")
def nba_lineups_all(d: str | None = None):
    """Resumo pra lista: quantos fora/dúvida por time em cada jogo do dia."""
    d = d or date.today().isoformat()
    out = {}
    for g in N.games_by_date(d):
        L = RW.lineups_for(g["away_abbr"], g["home_abbr"], g["away_id"], g["home_id"])
        def cnt(side):
            q = side["questionable"]
            out_n = sum(1 for p in q if p["inj"] in ("Out", "OFS")) or sum(1 for i in side["injuries"] if i["status"] == "Out")
            dq = sum(1 for p in q if p["inj"] in ("Ques", "Doubt", "GTD", "Prob"))
            return dict(out=out_n, doubt=dq)
        out[g["id"]] = dict(source=L["source"], status=L["status_home"] or L["status_away"], away=cnt(L["away"]), home=cnt(L["home"]))
    return out


# ======================================================================
#  ⭐ A BOA DO DIA — futebol
# ======================================================================
FOOT_PLAYER_PICKS = [("g_sot1", "1+ chute no gol", "shots_on", 0.5), ("g_shot2", "2+ chutes", "shots", 1.5), ("g_shot1", "1+ chute", "shots", 0.5),
                     ("g_foul2", "2+ faltas", "fouls", 1.5), ("g_foul1", "1+ falta", "fouls", 0.5), ("g_card", "Levar cartão", "cards", 0.5)]


@app.get("/api/best")
def best_football(d: str | None = None, leagues: str | None = None, venue: int = 0):
    d = d or date.today().isoformat()
    sel = set(leagues.split(",")) if leagues else set(E.DEFAULT_LEAGUES)
    rows = [r for r in E.fixtures_by_date(d) if r["league_id"] in sel and r["status"] != "FT"]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda f: (f, _analyze(f, venue)), rows))
    teams, players = [], []
    for f, (H, A) in res:
        base = dict(fixture_id=f["id"], date=f["date"], league=f["league_name"], flag=E.LEAGUES.get(f["league_id"], ("", ""))[1],
                    home=f["home_name"], away=f["away_name"], home_logo=f["home_logo"], away_logo=f["away_logo"])
        for gh, ga in zip(H["frequencies"], A["frequencies"]):
            for ih, ia in zip(gh["items"], ga["items"]):
                if ih["n"] >= 8 and ia["n"] >= 8 and ih["pct"] >= 70 and ia["pct"] >= 70:
                    pct = round(100 * (ih["hits"] + ia["hits"]) / (ih["n"] + ia["n"]))
                    if pct >= 80:
                        teams.append(dict(base, kind="team", group=gh["group"], label=ih["label"], pct=pct,
                                          home_hits=ih["hits"], home_n=ih["n"], away_hits=ia["hits"], away_n=ia["n"], score=pct + min(ih["pct"], ia["pct"]) / 10))
        for side, S, tname, tlogo in (("home", H, f["home_name"], f["home_logo"]), ("away", A, f["away_name"], f["away_logo"])):
            for p in S["players"]["all"]:
                # ESPN não informa minutos no futebol -> exige presença em ≥6 dos últimos jogos (≥60% dos jogos do time)
                if p["games"] < 6 or p["games"] < 0.6 * max(1, len(S["games"])):
                    continue
                for key, label, stat, line in FOOT_PLAYER_PICKS:
                    hits = p[key]
                    pct = round(100 * hits / p["games"])
                    if pct >= 80:
                        avg = p[stat] / p["games"]
                        players.append(dict(base, kind="player", player=p["name"], team=tname, team_logo=tlogo, side=side, label=label, stat=stat, line=line,
                                            hits=hits, n=p["games"], pct=pct, avg=round(avg, 2), score=pct + min(30, (avg - line) * 10),
                                            log=p["log"]))
                        break  # só a melhor regra por jogador
    teams.sort(key=lambda x: -x["score"]); players.sort(key=lambda x: -x["score"])
    return dict(date=d, teams=teams[:20], players=players[:20])


# ======================================================================
#  ⭐ A BOA DO DIA — NBA
# ======================================================================
NBA_MIN_LINE = dict(pts=9.5, reb=3.5, ast=2.5, tpm=1.5, pra=14.5)
NBA_LBL = dict(pts="Pontos", reb="Rebotes", ast="Assistências", tpm="Bolas de 3", pra="Pts+Reb+Ast")
_nba_an_cache = {}


def _nba_analyze(g):
    c = _nba_an_cache.get(g["id"])
    if c and time.time() - c[0] < 900:
        return c[1]
    with ThreadPoolExecutor(2) as ex:
        fh = ex.submit(N.team_history, g["home_id"], HISTORY_N)
        fa = ex.submit(N.team_history, g["away_id"], HISTORY_N)
        hh, ah = fh.result(), fa.result()
    H = N.summarize_team(hh, g["home_id"], HISTORY_N)
    A = N.summarize_team(ah, g["away_id"], HISTORY_N)
    _nba_an_cache[g["id"]] = (time.time(), (H, A))
    return H, A


@app.get("/api/nba/best")
def best_nba(d: str | None = None):
    d = d or date.today().isoformat()
    games = [g for g in N.games_by_date(d) if g["status"] != "FT"]
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda g: (g, _nba_analyze(g)), games))
    picks = []
    for g, (H, A) in res:
        L = RW.lineups_for(g["away_abbr"], g["home_abbr"], g["away_id"], g["home_id"])
        for side, S, tabbr, tname, tlogo in (("home", H, g["home_abbr"], g["home_name"], g["home_logo"]), ("away", A, g["away_abbr"], g["away_name"], g["away_logo"])):
            ls = L[side]
            out_names = {p["name"].lower() for p in ls["questionable"] if p["inj"] in ("Out", "OFS")} | {(i["name"] or "").lower() for i in ls["injuries"] if i["status"] == "Out"}
            dq_names = {p["name"].lower() for p in ls["questionable"] if p["inj"] in ("Ques", "Doubt", "GTD")}
            starters = {p["name"].lower() for p in ls["starters"]}
            for p in S["players"]:
                n = p["games"]
                if n < 8 or p["min"] < 22 or p["name"].lower() in out_names or p["old_games"] > 5:
                    continue
                best = None
                for stat, lbl in NBA_LBL.items():
                    st = p["stats"][stat]
                    line = st["safe8"]
                    if line is None or line < NBA_MIN_LINE[stat]:
                        continue
                    vals = [x[stat] for x in p["log"]]
                    hits = sum(1 for v in vals if v > line)
                    pct = round(100 * hits / n)
                    margin = (st["avg"] - line) / line
                    score = pct + min(25, margin * 50) + (5 if p["name"].lower() in starters else 0) - (10 if p["name"].lower() in dq_names else 0)
                    cand = dict(kind="player", game_id=g["id"], date=g["date"], matchup=f"{g['away_abbr']} @ {g['home_abbr']}", player=p["name"], pos=p["pos"],
                                team=tabbr, team_name=tname, team_logo=tlogo, opp=(g["away_abbr"] if side == "home" else g["home_abbr"]),
                                stat=stat, label=f"Over {line} {lbl}", line=line, hits=hits, n=n, pct=pct, avg=st["avg"], margin=round(margin * 100),
                                status=("titular" if p["name"].lower() in starters else "dúvida" if p["name"].lower() in dq_names else ""),
                                old_games=p["old_games"], score=round(score, 1), log=p["log"])
                    if best is None or cand["score"] > best["score"]:
                        best = cand
                if best:
                    picks.append(best)
    picks.sort(key=lambda x: -x["score"])
    return dict(date=d, picks=picks[:25])
