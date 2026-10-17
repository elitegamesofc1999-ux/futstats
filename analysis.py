"""Transforma o histórico de jogos de um time em médias e frequências."""
import json
from collections import defaultdict


def _g(stats, team_id, key):
    try:
        return float(stats[str(team_id)].get(key) or 0)
    except (KeyError, TypeError):
        return 0.0


def game_metrics(row, team_id):
    """Extrai métricas de UM jogo sob a ótica de `team_id`."""
    stats = json.loads(row["stats_json"]) if row["stats_json"] else {}
    home = row["home_id"] == team_id
    opp = row["away_id"] if home else row["home_id"]
    gf = (row["home_goals"] if home else row["away_goals"]) or 0
    ga = (row["away_goals"] if home else row["home_goals"]) or 0

    def pair(key):
        a, b = _g(stats, team_id, key), _g(stats, opp, key)
        return a, b, a + b

    corners = pair("Corner Kicks")
    yellows = pair("Yellow Cards")
    reds = pair("Red Cards")
    shots = pair("Total Shots")
    sot = pair("Shots on Goal")
    fouls = pair("Fouls")
    offs = pair("Offsides")
    xg = pair("expected_goals")
    ht_gf, ht_ga = _g(stats, team_id, "HT Goals"), _g(stats, opp, "HT Goals")
    ht_cards = pair("HT Cards")
    return dict(
        ht_gf=ht_gf, ht_ga=ht_ga, ht_goals=ht_gf + ht_ga, st_goals=gf + ga - ht_gf - ht_ga,
        ht_cards=ht_cards[2], ht_cards_for=ht_cards[0],
        fixture_id=row["id"], date=row["date"][:10], league=row["league_name"], home=home,
        opponent=row["away_name"] if home else row["home_name"],
        opponent_logo=row["away_logo"] if home else row["home_logo"],
        score=f"{row['home_goals']}-{row['away_goals']}",
        result="V" if gf > ga else ("E" if gf == ga else "D"),
        gf=gf, ga=ga, goals=gf + ga, btts=gf > 0 and ga > 0,
        corners_for=corners[0], corners_against=corners[1], corners=corners[2],
        yellow_for=yellows[0], yellow_against=yellows[1], yellow=yellows[2],
        red_for=reds[0], red_against=reds[1], red=reds[2],
        cards=yellows[2] + reds[2], cards_for=yellows[0] + reds[0],
        shots_for=shots[0], shots_against=shots[1], shots=shots[2],
        sot_for=sot[0], sot_against=sot[1], sot=sot[2],
        fouls_for=fouls[0], fouls_against=fouls[1], fouls=fouls[2],
        offsides_for=offs[0], offsides=offs[2],
        xg_for=xg[0], xg_against=xg[1],
        possession=_g(stats, team_id, "Ball Possession"),
    )


# (rótulo, chave do jogo, função de teste)  -> frequência "aconteceu em X de N jogos"
FREQ_RULES = [
    ("Gols", [
        ("Over 1.5 gols no jogo", lambda m: m["goals"] > 1.5),
        ("Over 2.5 gols no jogo", lambda m: m["goals"] > 2.5),
        ("Over 3.5 gols no jogo", lambda m: m["goals"] > 3.5),
        ("Ambas marcam", lambda m: m["btts"]),
        ("Time marcou", lambda m: m["gf"] > 0),
        ("Time marcou 2+", lambda m: m["gf"] >= 2),
        ("Time sofreu gol", lambda m: m["ga"] > 0),
        ("Venceu", lambda m: m["result"] == "V"),
    ]),
    ("1º tempo", [
        ("Over 0.5 gols no 1ºT", lambda m: m["ht_goals"] > 0.5),
        ("Over 1.5 gols no 1ºT", lambda m: m["ht_goals"] > 1.5),
        ("Time marcou no 1ºT", lambda m: m["ht_gf"] > 0),
        ("Over 0.5 gols no 2ºT", lambda m: m["st_goals"] > 0.5),
        ("Over 1.5 gols no 2ºT", lambda m: m["st_goals"] > 1.5),
        ("Houve cartão no 1ºT", lambda m: m["ht_cards"] > 0),
        ("Over 1.5 cartões no 1ºT", lambda m: m["ht_cards"] > 1.5),
    ]),
    ("Escanteios", [
        ("Over 8.5 no jogo", lambda m: m["corners"] > 8.5),
        ("Over 9.5 no jogo", lambda m: m["corners"] > 9.5),
        ("Over 10.5 no jogo", lambda m: m["corners"] > 10.5),
        ("Time 4+ escanteios", lambda m: m["corners_for"] >= 4),
        ("Time 5+ escanteios", lambda m: m["corners_for"] >= 5),
        ("Time 6+ escanteios", lambda m: m["corners_for"] >= 6),
    ]),
    ("Cartões", [
        ("Over 2.5 cartões no jogo", lambda m: m["cards"] > 2.5),
        ("Over 3.5 cartões no jogo", lambda m: m["cards"] > 3.5),
        ("Over 4.5 cartões no jogo", lambda m: m["cards"] > 4.5),
        ("Time 2+ cartões", lambda m: m["cards_for"] >= 2),
        ("Houve cartão vermelho", lambda m: m["red"] > 0),
    ]),
    ("Finalizações", [
        ("Time 10+ chutes", lambda m: m["shots_for"] >= 10),
        ("Time 13+ chutes", lambda m: m["shots_for"] >= 13),
        ("Time 4+ chutes no gol", lambda m: m["sot_for"] >= 4),
        ("Time 5+ chutes no gol", lambda m: m["sot_for"] >= 5),
        ("Over 8.5 chutes no gol (jogo)", lambda m: m["sot"] > 8.5),
        ("Over 24.5 chutes (jogo)", lambda m: m["shots"] > 24.5),
    ]),
    ("Faltas e impedimentos", [
        ("Time 10+ faltas", lambda m: m["fouls_for"] >= 10),
        ("Time 12+ faltas", lambda m: m["fouls_for"] >= 12),
        ("Over 22.5 faltas (jogo)", lambda m: m["fouls"] > 22.5),
        ("Time 2+ impedimentos", lambda m: m["offsides_for"] >= 2),
    ]),
]

def rule_id(group, label):
    import re, unicodedata
    t = unicodedata.normalize("NFKD", f"{group} {label}").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")


def all_markets():
    return [dict(group=g, items=[dict(id=rule_id(g, l), label=l) for l, _ in rules]) for g, rules in FREQ_RULES]


AVG_KEYS = [
    ("gf", "Gols marcados"), ("ga", "Gols sofridos"), ("goals", "Gols no jogo"), ("ht_goals", "Gols no 1ºT (jogo)"),
    ("corners_for", "Escanteios do time"), ("corners_against", "Escanteios do rival"), ("corners", "Escanteios no jogo"),
    ("cards_for", "Cartões do time"), ("cards", "Cartões no jogo"),
    ("shots_for", "Chutes do time"), ("shots_against", "Chutes sofridos"),
    ("sot_for", "Chutes no gol do time"), ("sot_against", "Chutes no gol sofridos"),
    ("fouls_for", "Faltas cometidas"), ("fouls_against", "Faltas sofridas"),
    ("offsides_for", "Impedimentos"), ("possession", "Posse de bola %"),
]


def summarize(rows, team_id, venue=None, n_max=10):
    """venue: None = todos os jogos; 'home' = só em casa; 'away' = só fora."""
    games = [game_metrics(r, team_id) for r in rows]
    if venue == "home":
        games = [g for g in games if g["home"]]
    elif venue == "away":
        games = [g for g in games if not g["home"]]
    games = games[:n_max]
    rows = [r for r in rows if r["id"] in {g["fixture_id"] for g in games}]
    n = len(games)
    avgs = {}
    for k, label in AVG_KEYS:
        avgs[k] = dict(label=label, value=round(sum(g[k] for g in games) / n, 2) if n else None)
    freqs = []
    for group, rules in FREQ_RULES:
        items = []
        for label, fn in rules:
            hits = sum(1 for g in games if fn(g))
            items.append(dict(id=rule_id(group, label), label=label, hits=hits, n=n, pct=round(100 * hits / n) if n else None))
        freqs.append(dict(group=group, items=items))

    # jogadores: agrega nos jogos que temos players_json
    agg = defaultdict(lambda: dict(games=0, minutes=0, shots=0, shots_on=0, goals=0, assists=0,
                                   fouls=0, fouled=0, yellow=0, red=0, tackles=0, key_passes=0,
                                   g_shot1=0, g_shot2=0, g_sot1=0, g_foul1=0, g_foul2=0, g_card=0, g_goal=0))
    pgames = 0
    for r in rows:
        if not r["players_json"]:
            continue
        plist = json.loads(r["players_json"]).get(str(team_id), [])
        if not plist:
            continue
        pgames += 1
        for p in plist:
            a = agg[(p["id"], p["name"], p.get("photo"))]
            a["games"] += 1
            for k in ("minutes", "shots", "shots_on", "goals", "assists", "fouls", "fouled", "yellow", "red", "tackles", "key_passes"):
                a[k] += p.get(k) or 0
            a["g_shot1"] += (p.get("shots") or 0) >= 1
            a["g_shot2"] += (p.get("shots") or 0) >= 2
            a["g_sot1"] += (p.get("shots_on") or 0) >= 1
            a["g_foul1"] += (p.get("fouls") or 0) >= 1
            a["g_foul2"] += (p.get("fouls") or 0) >= 2
            a["g_card"] += ((p.get("yellow") or 0) + (p.get("red") or 0)) >= 1
            a["g_goal"] += (p.get("goals") or 0) >= 1
    players = []
    for (pid, name, photo), a in agg.items():
        players.append(dict(id=pid, name=name, photo=photo, **a,
                            shots_pg=round(a["shots"] / a["games"], 2),
                            sot_pg=round(a["shots_on"] / a["games"], 2),
                            fouls_pg=round(a["fouls"] / a["games"], 2)))
    players = [p for p in players if p["games"] >= 2] or players
    top_shots = sorted(players, key=lambda p: (-p["shots"], -p["shots_on"]))[:8]
    top_fouls = sorted(players, key=lambda p: (-p["fouls"], -p["yellow"]))[:8]
    top_cards = sorted(players, key=lambda p: (-(p["yellow"] + p["red"]), -p["fouls"]))[:6]
    top_cards = [p for p in top_cards if p["yellow"] + p["red"] > 0]

    return dict(n=n, games=games, averages=avgs, frequencies=freqs,
                players=dict(games=pgames, top_shots=top_shots, top_fouls=top_fouls, top_cards=top_cards))
