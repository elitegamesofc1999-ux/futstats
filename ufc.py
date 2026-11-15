"""UFC — fonte ESPN (site API para cards + core API para luta a luta)."""
import json
import os
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc"
CORE = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ufc.db")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_HOURS", "-3"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FutStats/1.0"}
HIST_N = 6  # últimas lutas analisadas por lutador


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as con:
        con.executescript("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, saved_at INTEGER);")


def cache_get(key, max_age):
    with db() as con:
        r = con.execute("SELECT value, saved_at FROM cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r["saved_at"] < max_age:
        return json.loads(r["value"])
    return None


def cache_set(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, json.dumps(value), int(time.time())))


def get(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def ref(url):
    """GET numa $ref da core API."""
    return get(url.replace("http://", "https://"))


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _dt(s):
    return datetime.strptime(s[:17], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


# ------------------------------------------------------------ eventos / card
def _fighter_from_site(x):
    a = x.get("athlete", {})
    rec = next((r["summary"] for r in x.get("records", []) if r.get("type") == "total"), None)
    return dict(id=int(x["id"]), name=a.get("displayName"), short=a.get("shortName"), flag=(a.get("flag") or {}).get("href"),
                country=(a.get("flag") or {}).get("alt"), record=rec, winner=x.get("winner"),
                headshot=f"https://a.espncdn.com/i/headshots/mma/players/full/{x['id']}.png")


def _event_rows(data):
    out = []
    for e in data.get("events", []):
        comps = e.get("competitions", [])
        st = e["status"]["type"]
        dt = _dt(e["date"])
        fights = []
        for c in comps:
            cs = c["status"]["type"]
            fighters = [_fighter_from_site(x) for x in c.get("competitors", [])]
            fights.append(dict(
                id=int(c["id"]), weight=(c.get("type") or {}).get("abbreviation", ""), rounds=((c.get("format") or {}).get("regulation") or {}).get("periods", 3),
                status="FT" if cs.get("completed") else ("NS" if cs.get("state") == "pre" else "LIVE"), status_detail=cs.get("shortDetail", ""),
                period=c["status"].get("period"), clock=c["status"].get("displayClock"), fighters=fighters,
                date=_dt(c["date"]).astimezone(LOCAL_TZ).isoformat() if c.get("date") else None,
            ))
        out.append(dict(id=int(e["id"]), name=e["name"], short=e.get("shortName"), date=dt.astimezone(LOCAL_TZ).isoformat(), ts=int(dt.timestamp()),
                        status="FT" if st.get("completed") else ("NS" if st.get("state") == "pre" else "LIVE"),
                        venue=((comps[0].get("venue") or {}).get("fullName") if comps else None), n_fights=len(fights), fights=fights))
    return out


def events(days_back=10, days_fwd=70):
    """Eventos UFC recentes e próximos."""
    key = f"events:{days_back}:{days_fwd}"
    c = cache_get(key, 900)
    if c is not None:
        return c
    today = datetime.now(LOCAL_TZ).date()
    rng = f"{(today - timedelta(days=days_back)):%Y%m%d}-{(today + timedelta(days=days_fwd)):%Y%m%d}"
    try:
        rows = _event_rows(get(f"{SITE}/scoreboard", dates=rng))
    except Exception:
        rows = []
    rows.sort(key=lambda r: r["ts"])
    slim = [{k: v for k, v in r.items() if k != "fights"} for r in rows]
    cache_set(key, slim)
    for r in rows:
        cache_set(f"event:{r['id']}", r)
    return slim


def event_card(event_id):
    key = f"event:{event_id}"
    c = cache_get(key, 900)
    if c is None:
        events()
        c = cache_get(key, 900)
    if c is None:
        raise ValueError("evento não encontrado")
    # ordem do card + segmento (Main Card / Prelims) vem da core API
    seg = _segments(event_id)
    for f in c["fights"]:
        s = seg.get(str(f["id"]), {})
        f["segment"] = s.get("segment", "")
        f["order"] = s.get("order", 99)
    c["fights"].sort(key=lambda f: f["order"])
    return c


def _segments(event_id):
    key = f"seg:{event_id}"
    c = cache_get(key, 6 * 3600)
    if c is not None:
        return c
    out = {}
    try:
        d = get(f"{CORE}/events/{event_id}")
        for comp in d.get("competitions", []):
            out[str(comp["id"])] = dict(order=comp.get("matchNumber", 99), segment=((comp.get("cardSegment") or {}).get("description") or ""))
    except Exception:
        pass
    cache_set(key, out)
    return out


def fight_odds(event_id, fight_id):
    key = f"odds:{fight_id}"
    c = cache_get(key, 1800)
    if c is not None:
        return c
    odds = None
    try:
        d = get(f"{CORE}/events/{event_id}/competitions/{fight_id}/odds")
        it = (d.get("items") or [None])[0]
        if it:
            odds = dict(provider=(it.get("provider") or {}).get("name"), details=it.get("details"), rounds_ou=it.get("overUnder"),
                        over_odds=it.get("overOdds"), under_odds=it.get("underOdds"),
                        ml_a=(it.get("awayAthleteOdds") or {}).get("moneyLine"), ml_b=(it.get("homeAthleteOdds") or {}).get("moneyLine"))
    except Exception:
        pass
    cache_set(key, odds)
    return odds


# ------------------------------------------------------------ lutador
def profile(fid):
    key = f"prof:{fid}"
    c = cache_get(key, 24 * 3600)
    if c is not None:
        return c
    out = dict(id=fid)
    try:
        a = get(f"{WEB}/athletes/{fid}").get("athlete", {})
        ss = {s["name"]: s["displayValue"] for s in (a.get("statsSummary") or {}).get("statistics", [])}
        out.update(name=a.get("displayName"), headshot=(a.get("headshot") or {}).get("href"), height=a.get("displayHeight"), weight=a.get("displayWeight"),
                   reach=a.get("displayReach"), stance=(a.get("stance") or {}).get("text"), age=a.get("age"), country=(a.get("citizenshipCountry") or {}).get("name") or a.get("citizenship"),
                   flag=(a.get("flag") or {}).get("href"), weight_class=(a.get("weightClass") or {}).get("text"), style=a.get("displayFightingStyle"),
                   record=ss.get("wins-losses-draws"), ko=ss.get("tkos-tkoLosses"), sub=ss.get("submissions-submissionLosses"), association=a.get("association", {}).get("name") if isinstance(a.get("association"), dict) else None)
    except Exception:
        pass
    cache_set(key, out)
    return out


def _athlete_name(url):
    aid = url.split("/athletes/")[1].split("?")[0]
    key = f"aname:{aid}"
    c = cache_get(key, 90 * 86400)
    if c is not None:
        return c
    try:
        d = ref(url)
        c = dict(id=int(aid), name=d.get("displayName"), flag=(d.get("flag") or {}).get("href"))
    except Exception:
        c = dict(id=int(aid), name="?", flag=None)
    cache_set(key, c)
    return c


STAT_KEYS = ["knockDowns", "sigStrikesLanded", "sigStrikesAttempted", "totalStrikesLanded", "takedownsLanded", "takedownsAttempted", "submissions", "timeInControl", "reversals"]


def _stats(url):
    try:
        d = ref(url)
        out = {}
        for cat in d["splits"]["categories"]:
            for s in cat["stats"]:
                if s["name"] in STAT_KEYS:
                    out[s["name"]] = s.get("value") or 0
        return out
    except Exception:
        return None


def _method(result):
    name = (result or {}).get("name", "") or ""
    desc = (result or {}).get("displayDescription") or (result or {}).get("description") or ""
    n = name.lower()
    if "decision" in n:
        m = "Decisão"
        if "unanimous" in n:
            m = "Decisão unânime"
        elif "split" in n:
            m = "Decisão dividida"
        elif "majority" in n:
            m = "Decisão majoritária"
        return "DEC", m
    if "submission" in n:
        return "SUB", f"Finalização ({desc})" if desc else "Finalização"
    if "ko" in n or "tko" in n or "knockout" in n:
        return "KO", f"KO/TKO ({desc})" if desc else "KO/TKO"
    if "dq" in n or "disqual" in n:
        return "DQ", "Desqualificação"
    if "no contest" in n or n == "nc":
        return "NC", "No contest"
    if "draw" in n:
        return "DRAW", "Empate"
    return "OTHER", name or "—"


def _one_fight(fid, item):
    """Monta o registro de uma luta passada do lutador `fid`."""
    cid = item["competition"]["$ref"].split("/competitions/")[1].split("?")[0]
    key = f"fight:{fid}:{cid}"
    c = cache_get(key, 365 * 86400)
    if c is not None:
        return c
    try:
        comp = ref(item["competition"]["$ref"])
        st = ref(comp["status"]["$ref"])
        me = next(x for x in comp["competitors"] if str(x["id"]) == str(fid))
        opp = next(x for x in comp["competitors"] if str(x["id"]) != str(fid))
        oppn = _athlete_name(opp["athlete"]["$ref"])
        with ThreadPoolExecutor(2) as ex:
            fs = ex.submit(_stats, me["statistics"]["$ref"]) if "statistics" in me else None
            fo = ex.submit(_stats, opp["statistics"]["$ref"]) if "statistics" in opp else None
            ms = fs.result() if fs else None
            os_ = fo.result() if fo else None
        periods = ((comp.get("format") or {}).get("regulation") or {}).get("periods", 3)
        period = st.get("period") or 0
        clock = st.get("clock")
        code, method = _method(st.get("result"))
        # tempo total da luta em segundos (clock = tempo restante no round)
        if code == "DEC":
            secs = periods * 300
            ended_round = periods
        else:
            ended_round = period or periods
            secs = (ended_round - 1) * 300 + (300 - (clock if clock is not None else 0))
        ev_name = ""
        try:
            ev_name = ref(item["event"]["$ref"]).get("name", "")
        except Exception:
            pass
        c = dict(fight_id=int(cid), date=_dt(comp["date"]).astimezone(LOCAL_TZ).isoformat()[:10], event=ev_name,
                 opp=oppn["name"], opp_id=oppn["id"], opp_flag=oppn["flag"], weight=(comp.get("type") or {}).get("abbreviation", ""),
                 win=bool(me.get("winner")), code=code, method=method, round=ended_round, time=st.get("displayClock"), secs=secs, rounds=periods,
                 distance=code == "DEC", finish=code in ("KO", "SUB"), finished_win=bool(me.get("winner")) and code in ("KO", "SUB"),
                 kd=(ms or {}).get("knockDowns"), ss=(ms or {}).get("sigStrikesLanded"), ssa=(ms or {}).get("sigStrikesAttempted"),
                 ts=(ms or {}).get("totalStrikesLanded"), td=(ms or {}).get("takedownsLanded"), tda=(ms or {}).get("takedownsAttempted"),
                 sub_att=(ms or {}).get("submissions"), ctrl=(ms or {}).get("timeInControl"),
                 opp_ss=(os_ or {}).get("sigStrikesLanded"), opp_td=(os_ or {}).get("takedownsLanded"), opp_kd=(os_ or {}).get("knockDowns"),
                 has_stats=ms is not None and ("sigStrikesLanded" in ms))
    except Exception as e:
        c = None
    cache_set(key, c)
    return c


def history(fid, n=HIST_N):
    key = f"hist:{fid}"
    c = cache_get(key, 12 * 3600)
    if c is not None and len(c) >= min(n, len(c)):
        return c[:n]
    try:
        d = get(f"{CORE}/athletes/{fid}/eventlog")
        items = [i for i in d.get("events", {}).get("items", []) if i.get("played")]
    except Exception:
        items = []
    items = items[: n + 2]
    with ThreadPoolExecutor(6) as ex:
        rows = [r for r in ex.map(lambda it: _one_fight(fid, it), items) if r]
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows = rows[:n]
    cache_set(key, rows)
    return rows


def _safe_line(values, min_hits):
    if len(values) < min_hits:
        return None
    v = sorted(values, reverse=True)
    thr = v[min_hits - 1]
    return int(thr) - 0.5 if thr >= 1 else None


FIGHT_RULES = [
    ("Duração da luta", [("Passou do 1º round", lambda f: f["secs"] > 300), ("Passou de 1.5 rounds", lambda f: f["secs"] > 450),
                         ("Passou de 2.5 rounds", lambda f: f["secs"] > 750), ("Foi até a decisão", lambda f: f["distance"]),
                         ("Acabou antes do fim (KO/SUB)", lambda f: f["finish"])]),
    ("Resultado", [("Venceu", lambda f: f["win"]), ("Venceu por KO/TKO", lambda f: f["win"] and f["code"] == "KO"),
                   ("Venceu por finalização", lambda f: f["win"] and f["code"] == "SUB"), ("Venceu por decisão", lambda f: f["win"] and f["code"] == "DEC"),
                   ("Venceu no 1º round", lambda f: f["win"] and f["finish"] and f["round"] == 1)]),
    ("Golpes significativos", [(f"Over {l}", (lambda l: lambda f: (f["ss"] or 0) > l)(l)) for l in (19.5, 29.5, 39.5, 49.5, 69.5, 89.5)]),
    ("Quedas", [("1+ queda", lambda f: (f["td"] or 0) >= 1), ("2+ quedas", lambda f: (f["td"] or 0) >= 2), ("3+ quedas", lambda f: (f["td"] or 0) >= 3),
                ("Knockdown", lambda f: (f["kd"] or 0) >= 1)]),
]
PLAYER_STATS = [("ss", "Golpes significativos"), ("ssa", "Golpes sig. tentados"), ("ts", "Golpes totais"), ("td", "Quedas"), ("tda", "Quedas tentadas"),
                ("kd", "Knockdowns"), ("ctrl_min", "Controle (min)"), ("rounds_done", "Rounds (duração)"), ("opp_ss", "Golpes sofridos"), ("sub_att", "Tentativas de finalização")]


def summarize_fighter(fid, n=HIST_N):
    prof = profile(fid)
    fights = history(fid, n)
    for f in fights:
        f["ctrl_min"] = round((f["ctrl"] or 0) / 60, 1) if f.get("ctrl") is not None else None
        f["rounds_done"] = round(f["secs"] / 300, 2)
    N = len(fights)
    withs = [f for f in fights if f["has_stats"]]
    stats = {}
    for k, lbl in PLAYER_STATS:
        src = fights if k == "rounds_done" else withs
        vals = [f[k] for f in src if f.get(k) is not None]
        if vals:
            stats[k] = dict(label=lbl, n=len(vals), avg=round(sum(vals) / len(vals), 1), max=max(vals), min=min(vals), med=statistics.median(vals),
                            safe8=_safe_line(vals, max(1, round(len(vals) * 0.8))), safe7=_safe_line(vals, max(1, round(len(vals) * 0.7))))
        else:
            stats[k] = dict(label=lbl, n=0, avg=None, max=None, min=None, med=None, safe8=None, safe7=None)
    freqs = []
    for group, rules in FIGHT_RULES:
        items = []
        for l, fn in rules:
            src = withs if group in ("Golpes significativos", "Quedas") else fights
            hits = sum(1 for f in src if fn(f))
            items.append(dict(label=l, hits=hits, n=len(src), pct=round(100 * hits / len(src)) if src else None))
        freqs.append(dict(group=group, items=items))
    wins = sum(1 for f in fights if f["win"])
    nc = sum(1 for f in fights if not f["win"] and f["code"] in ("NC", "DRAW"))
    streak = 0
    for f in fights:
        if f["win"]:
            streak += 1
        else:
            break
    return dict(profile=prof, n=N, fights=fights, stats=stats, frequencies=freqs,
                summary=dict(wins=wins, losses=N - wins - nc, nc=nc, streak=streak, finishes=sum(1 for f in fights if f["finished_win"]),
                             avg_secs=round(sum(f["secs"] for f in fights) / N) if N else None,
                             avg_rounds=round(sum(f["secs"] for f in fights) / N / 300, 2) if N else None,
                             last_date=fights[0]["date"] if fights else None))


def fight_detail(event_id, fight_id):
    card = event_card(event_id)
    f = next((x for x in card["fights"] if x["id"] == fight_id), None)
    if not f:
        raise ValueError("luta não encontrada")
    with ThreadPoolExecutor(2) as ex:
        fa = ex.submit(summarize_fighter, f["fighters"][0]["id"])
        fb = ex.submit(summarize_fighter, f["fighters"][1]["id"])
        A, B = fa.result(), fb.result()
    odds = fight_odds(event_id, fight_id) if f["status"] != "FT" else None
    return dict(event={k: v for k, v in card.items() if k != "fights"}, fight=f, a=A, b=B, odds=odds, history_n=HIST_N)


def all_markets():
    return [dict(group=g, items=[dict(id=f"{gi}_{i}", label=l) for i, (l, _) in enumerate(rules)]) for gi, (g, rules) in enumerate(FIGHT_RULES)]
