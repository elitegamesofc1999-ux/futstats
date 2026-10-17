# FutStats — tendências dos últimos 10 jogos

Site que lista os jogos do dia (Premier League, La Liga, Serie A, Bundesliga,
Ligue 1, Champions, Europa League e Brasileirão) e, ao clicar num jogo, compara
os dois times com base nos últimos 10 jogos de cada um (liga + copas + continentais):

- Médias: gols, escanteios, cartões, chutes, chutes no gol, faltas, impedimentos, posse
- Frequências ("aconteceu em X de 10"): Over 1.5/2.5/3.5 gols, ambas marcam,
  escanteios Over 8.5/9.5/10.5, cartões Over 2.5/3.5/4.5, chutes, faltas…
- Jogadores que mais finalizam / cometem faltas / levam cartão
- Tabela dos últimos jogos analisados

Fonte dos dados: API pública da ESPN (sem chave, sem limite).

## Como usar

1. Instale o Python em https://www.python.org/downloads/ (marque "Add Python to PATH")
2. Windows: dois cliques em `INICIAR.bat`  ·  Mac/Linux: `./INICIAR_mac_linux.sh`
3. Abre sozinho em http://localhost:8000

## Estrutura

- `espn.py` — busca jogos/estatísticas na ESPN e guarda em `data/espn.db`
- `analysis.py` — médias, frequências e agregados de jogadores
- `main.py` — servidor (FastAPI): `/api/today`, `/api/match/{id}`
- `static/index.html` — a tela (pode editar à vontade)
