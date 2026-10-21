"""Escalação provável da NBA (RotoWire) + lesões oficiais (ESPN) como reserva."""
import json
import re
import time

import requests

RW_URL = "https://www.rotowire.com/basketball/nba-lineups.php"
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}

# RotoWire -> ESPN
ABBR = {"WAS": "WSH", "NY": "NYK", "GS": "GSW", "SA": "SAS", "NO": "NOP", "UTA": "UTAH", "PHO": "PHX", "BKN": "BKN", "CHA": "CHA", "OKC": "OKC"}
STATUS_PT = {"Out": "Fora", "Doubt": "Dúvida (improvável)", "Ques": "Dúvida", "Prob": "Provável", "OFS": "Fora da temporada", "GTD": "Dúvida (decisão no dia)"}

_cache = {"ts": 0, "data": None}
_inj_cache = {"ts": 0, "data": None}


def norm_abbr(a):
    a = (a or "").upper()
    return ABBR.get(a, a)


def _clean(s):
    return " ".join(re.sub(r"<[^>]+>", " ", s).split())


def _parse_list(ul):
    """Lista <ul class=lineup__list> -> dict(status, starters[], questionable[])"""
    out = dict(status="", starters=[], questionable=[])
    m = re.search(r'lineup__status[^"]*">(.*?)</li>', ul, re.S)
    if m:
        out["status"] = _clean(m.group(1))
    in_q = False
    for li in re.findall(r"<li[^>]*>.*?</li>", ul, re.S):
        if "lineup__title" in li:
            in_q = True
            continue
        if "lineup__player" not in li:
            continue
        cls = re.search(r'class="lineup__player([^"]*)"', li).group(1)
        pct = re.search(r"is-pct-play-(\d+)", cls)
        pos = re.search(r'lineup__pos"[^>]*>([^<]*)', li)
        name = re.search(r'<a title="([^"]*)"', li)
        inj = re.search(r'lineup__inj">([^<]*)', li)
        p = dict(name=name.group(1) if name else _clean(li), pos=(pos.group(1).strip() if pos else ""),
                 pct=int(pct.group(1)) if pct else None, inj=(inj.group(1).strip() if inj else ""))
        p["inj_pt"] = STATUS_PT.get(p["inj"], p["inj"])
        (out["questionable"] if (in_q or "has-injury-status" in cls and inj) else out["starters"]).append(p)
    return out


def fetch_rotowire(max_age=900):
    if _cache["data"] is not None and time.time() - _cache["ts"] < max_age:
        return _cache["data"]
    games = {}
    try:
        h = requests.get(RW_URL, headers=HEADERS, timeout=25).text
        boxes = [x for x in re.split(r'(?=<div class="lineup__box">)', h) if "lineup__matchup" in x]
        for x in boxes:
            abbrs = re.findall(r'lineup__abbr">([^<]+)', x)
            if len(abbrs) < 2:
                continue
            visit, home = norm_abbr(abbrs[0].strip()), norm_abbr(abbrs[1].strip())
            tm = re.search(r'lineup__time">([^<]*)', x)
            mv = re.search(r'<ul class="lineup__list is-visit">(.*?)</ul>', x, re.S)
            mh = re.search(r'<ul class="lineup__list is-home">(.*?)</ul>', x, re.S)
            if not (mv and mh):
                continue
            games[f"{visit}@{home}"] = dict(visit=visit, home=home, time_et=tm.group(1).strip() if tm else "",
                                             away=_parse_list(mv.group(1)), home_l=_parse_list(mh.group(1)))
        _cache.update(ts=time.time(), data=games)
    except Exception:
        if _cache["data"] is None:
            _cache.update(ts=time.time(), data={})
    return _cache["data"]


def fetch_espn_injuries(max_age=1800):
    if _inj_cache["data"] is not None and time.time() - _inj_cache["ts"] < max_age:
        return _inj_cache["data"]
    out = {}
    try:
        d = requests.get(ESPN_INJ, headers=HEADERS, timeout=25).json()
        for t in d.get("injuries", []):
            tid = str(t.get("id"))
            lst = []
            for i in t.get("injuries", []):
                a = i.get("athlete", {})
                lst.append(dict(name=a.get("displayName"), pos=(a.get("position") or {}).get("abbreviation", ""),
                                status=i.get("status"), status_pt=STATUS_PT.get({"Day-To-Day": "GTD", "Out": "Out", "Questionable": "Ques", "Doubtful": "Doubt", "Probable": "Prob"}.get(i.get("status"), i.get("status")), i.get("status")),
                                detail=(i.get("details") or {}).get("type") or "", comment=(i.get("shortComment") or "")[:160],
                                return_date=(i.get("details") or {}).get("returnDate")))
            out[tid] = lst
        _inj_cache.update(ts=time.time(), data=out)
    except Exception:
        if _inj_cache["data"] is None:
            _inj_cache.update(ts=time.time(), data={})
    return _inj_cache["data"]


def lineups_for(away_abbr, home_abbr, away_id, home_id):
    """Junta RotoWire (escalação) + ESPN (lesões) para um confronto."""
    rw = fetch_rotowire()
    key = f"{norm_abbr(away_abbr)}@{norm_abbr(home_abbr)}"
    g = rw.get(key)
    inj = fetch_espn_injuries()
    res = dict(source="RotoWire" if g else None, status_away=None, status_home=None,
               away=dict(starters=[], questionable=[], injuries=inj.get(str(away_id), [])),
               home=dict(starters=[], questionable=[], injuries=inj.get(str(home_id), [])))
    if g:
        res["status_away"], res["status_home"] = g["away"]["status"], g["home_l"]["status"]
        res["away"]["starters"], res["away"]["questionable"] = g["away"]["starters"], g["away"]["questionable"]
        res["home"]["starters"], res["home"]["questionable"] = g["home_l"]["starters"], g["home_l"]["questionable"]
        res["time_et"] = g["time_et"]
    return res
