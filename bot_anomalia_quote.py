import os
import time
import csv
import re
import unicodedata
from io import StringIO
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from difflib import SequenceMatcher

import logging
import requests

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("odds-anomaly-bot")

# =========================
# Environment (come la tua base, con poche aggiunte)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.getenv("RAPIDAPI_HOST", "bet365data.p.rapidapi.com")
RAPIDAPI_BASE  = os.getenv("RAPIDAPI_BASE", f"https://{RAPIDAPI_HOST}")

# live events (come tua base)
RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

# NEW: endpoint quote evento (ADATTA QUI path e parametri)
# Esempi:
#   - path con query ?event_id=xxx   -> RAPIDAPI_ODDS_QUERY_KEY=event_id
#   - path con segmento /{id}        -> lascia la query key vuota e usa il format nel build_url
RAPIDAPI_ODDS_PATH      = os.getenv("RAPIDAPI_ODDS_PATH", "/event-odds")
RAPIDAPI_ODDS_QUERY_KEY = os.getenv("RAPIDAPI_ODDS_QUERY_KEY", "event_id")  # "" se non serve query

GITHUB_CSV_URL      = os.getenv("GITHUB_CSV_URL", "")
MINUTE_CUTOFF       = int(os.getenv("MINUTE_CUTOFF", "35"))   # limite 35'
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))  # polling più reattivo dopo gol
SEND_STARTUP_MESSAGE = os.getenv("SEND_STARTUP_MESSAGE", "1") == "1"
DEBUG_LOG = os.getenv("DEBUG_LOG", "0") == "1"

# (Opzionale) Esclusioni campionati come tua base
LEAGUE_EXCLUDE_KEYWORDS = [kw.strip().lower() for kw in os.getenv(
    "LEAGUE_EXCLUDE_KEYWORDS", "Esoccer,Volta,8 mins play,H2H GG"
).split(",") if kw.strip()]

# =========================
# Stato: per ogni evento teniamo traccia di gol e quote
# =========================
class GoalState:
    __slots__ = (
        "last_score","waiting_relist","scoring_team",
        "baseline_odds","notified","last_seen_minute","last_suspended"
    )
    def __init__(self, score_tuple=(0,0), minute=0):
        self.last_score = score_tuple       # (home, away)
        self.waiting_relist = False         # true dopo il gol finché non riappaiono quote
        self.scoring_team = None            # "home" | "away"
        self.baseline_odds = None           # prima quota post-gol della squadra che ha segnato
        self.notified = False               # una sola notifica per episodio
        self.last_seen_minute = minute
        self.last_suspended = None          # bool/None

# key: event_id (se disponibile) altrimenti fallback su "home|away|league"
match_state: dict[str, GoalState] = {}

# =========================
# Telegram
# =========================
def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_TOKEN/CHAT_ID mancanti.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        if r.ok:
            logger.info("Telegram: messaggio inviato")
            return True
        logger.error("Telegram %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram exception: %s", e)
    return False

# =========================
# HTTP helper
# =========================
def http_get(url, headers=None, params=None, timeout=25):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if not r.ok:
            logger.error("HTTP %s %s | body: %s", r.status_code, url, r.text[:300])
        return r
    except Exception as e:
        logger.error("HTTP exception on %s: %s", url, e)
        return None

def build_url(path: str, **fmt):
    base = RAPIDAPI_BASE.rstrip("/")
    p = path.format(**fmt).lstrip("/")
    return f"{base}/{p}"

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# CSV (riuso dalla tua base per ricavare minute)
# =========================
def load_csv_from_github():
    if not GITHUB_CSV_URL:
        return []
    try:
        logger.info("Scarico CSV: %s", GITHUB_CSV_URL)
        r = requests.get(GITHUB_CSV_URL, timeout=30)
        r.raise_for_status()
        rows = list(csv.DictReader(StringIO(r.text)))
        logger.info("CSV caricato (%d righe)", len(rows))
        return rows
    except Exception as e:
        logger.exception("Errore caricamento CSV: %s", e)
        return []

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))

def norm_text(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[’'`]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

STOPWORDS = {
    "fc","cf","sc","ac","club","cd","de","del","da","do","d","u19","u20","u21","u23",
    "b","ii","iii","women","w","reserves","team","sv","afc","youth","if","fk"
}

def team_tokens(name: str) -> set[str]:
    toks = [t for t in norm_text(name).split() if t and t not in STOPWORDS]
    toks = [t for t in toks if len(t) >= 3 or t.isdigit()]
    return set(toks)

def token_match(a: str, b: str) -> bool:
    A, B = team_tokens(a), team_tokens(b)
    if not A or not B:
        return False
    if A == B or A.issubset(B) or B.issubset(A):
        return True
    inter = A & B
    if len(A) == 1 or len(B) == 1:
        return len(inter) >= 1
    return len(inter) >= 2

def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()

def match_teams(csv_match, live_match) -> bool:
    csv_home = csv_match.get("Home Team") or csv_match.get("Home") or csv_match.get("home") or ""
    csv_away = csv_match.get("Away Team") or csv_match.get("Away") or csv_match.get("away") or ""
    live_home = live_match.get("home","")
    live_away = live_match.get("away","")

    if token_match(csv_home, live_home) and token_match(csv_away, live_away):
        return True

    rh = fuzzy_ratio(csv_home, live_home)
    ra = fuzzy_ratio(csv_away, live_away)
    return (rh >= 0.72 and ra >= 0.60) or (rh >= 0.60 and ra >= 0.72)

def kickoff_minute_from_csv(csv_match) -> int | None:
    candidate_keys = ["timestamp","epoch","unix","Date Unix","Kickoff Unix","start_time","start","time_unix"]
    epoch_val = None

    for k in candidate_keys:
        if k in csv_match and str(csv_match[k]).strip():
            try:
                n = int(float(str(csv_match[k]).strip()))
                if n >= 1_000_000_000:
                    epoch_val = n
                    break
            except Exception:
                pass

    if epoch_val is None:
        try:
            first_key = next(iter(csv_match.keys()))
            n = int(float(str(csv_match[first_key]).strip()))
            if n >= 1_000_000_000:
                epoch_val = n
        except Exception:
            pass

    if epoch_val is None:
        return None

    now_utc = datetime.now(timezone.utc).timestamp()
    minute = int(max(0, (now_utc - epoch_val) // 60))
    return min(minute, 180)

# =========================
# Live events (RapidAPI) — ADATTA QUI il parsing (id, SS, squadre, lega)
# =========================
def get_live_matches():
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=25)
    if not r or not r.ok:
        return []
    try:
        data = r.json() or {}
    except Exception:
        logger.error("Response non-JSON: %s", r.text[:300]); return []

    root = data.get("data") or {}
    raw = root.get("events") or data.get("events") or []
    events = []

    for it in raw:
        # id evento (prova più chiavi comuni)
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or it.get("fixtureId") or "")
        league = (it.get("league") or it.get("CT") or it.get("competition") or "N/A").strip()
        home   = (it.get("home") or it.get("HomeTeam") or it.get("homeTeam") or "").strip()
        away   = (it.get("away") or it.get("AwayTeam") or it.get("awayTeam") or "").strip()
        score  = (it.get("SS") or it.get("score") or "").strip()  # es. "1-0"

        if any(ex in league.lower() for ex in LEAGUE_EXCLUDE_KEYWORDS):
            continue
        if home and away and score is not None:
            events.append({
                "id": event_id, "home": home, "away": away,
                "league": league, "SS": score
            })

    logger.info("API live-events: %d match live", len(events))
    return events

# =========================
# Odds 1X2 (RapidAPI) — ADATTA QUI il parsing a seconda della tua API
# =========================
def get_event_odds_1x2(event_id: str):
    """
    Ritorna dict:
      {"home": float|None, "draw": float|None, "away": float|None, "suspended": bool|None}
    """
    if not event_id:
        return None

    params = None
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    if RAPIDAPI_ODDS_QUERY_KEY:
        params = {RAPIDAPI_ODDS_QUERY_KEY: event_id}

    r = http_get(url, headers=HEADERS, params=params, timeout=20)
    if not r or not r.ok:
        return None

    try:
        data = r.json() or {}
    except Exception:
        logger.error("Odds non-JSON: %s", r.text[:300]); return None

    # esempi possibili:
    # data = {"markets":[{"key":"1x2","suspended":false,"outcomes":{"home":1.36,"draw":4.50,"away":7.00}}]}
    home = draw = away = None
    suspended = None

    markets = data.get("markets") or data.get("data") or []
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("list") or [markets]

    pick = None
    for m in markets:
        key = (str(m.get("key") or m.get("name") or m.get("market") or "")).lower()
        if "1x2" in key or key in ("match_result", "full_time_result", "ft_result", "result"):
            pick = m
            break
    if not pick and markets:
        # fallback: se la tua API ha un solo mercato quando chiedi 1X2
        pick = markets[0]

    if pick:
        # suspended
        if "suspended" in pick:
            suspended = bool(pick.get("suspended"))
        elif "Suspended" in pick:
            suspended = bool(pick.get("Suspended"))
        # outcomes
        oc = pick.get("outcomes") or pick.get("runners") or pick.get("outcome") or {}
        if isinstance(oc, dict):
            home = _to_float(oc.get("home") or oc.get("1") or oc.get("Home") or oc.get("team1"))
            draw = _to_float(oc.get("draw") or oc.get("X") or oc.get("Draw"))
            away = _to_float(oc.get("away") or oc.get("2") or oc.get("Away") or oc.get("team2"))
        elif isinstance(oc, list):
            name_map = {}
            for o in oc:
                name = str(o.get("name") or o.get("selection") or "").lower()
                price = o.get("price") or o.get("odds") or o.get("decimal") or o.get("Decimal")
                name_map[name] = _to_float(price)
            home = name_map.get("home") or name_map.get("1") or name_map.get("team1")
            draw = name_map.get("draw") or name_map.get("x")
            away = name_map.get("away") or name_map.get("2") or name_map.get("team2")

    return {"home": home, "draw": draw, "away": away, "suspended": suspended}

def _to_float(x):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None

# =========================
# Utils score + minute
# =========================
def parse_score_tuple(ss: str) -> tuple[int,int]:
    if not ss:
        return (0,0)
    parts = re.findall(r"\d+", ss)
    if len(parts) >= 2:
        try:
            return (int(parts[0]), int(parts[1]))
        except Exception:
            return (0,0)
    return (0,0)

def detect_scorer(prev: tuple[int,int], cur: tuple[int,int]) -> str | None:
    ph, pa = prev
    ch, ca = cur
    if (ch, ca) == (ph, pa):
        return None
    if ch == ph + 1 and ca == pa:
        return "home"
    if ca == pa + 1 and ch == ph:
        return "away"
    return None  # altro (autogol/correzioni)

# =========================
# Core
# =========================
def main_loop():
    logger.info("Bot avviato (logica: quota sale dopo gol entro %d')", MINUTE_CUTOFF)
    if SEND_STARTUP_MESSAGE:
        send_telegram_message("🤖 <b>Bot quote avviato</b>\nMonitoraggio anomalie post-gol…")

    csv_rows = load_csv_from_github()

    while True:
        try:
            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            # per ogni match live, prova ad agganciare al CSV (per il minuto)
            for lm in live:
                home, away, league = lm["home"], lm["away"], lm["league"]
                score_str = lm.get("SS") or ""
                cur_score = parse_score_tuple(score_str)

                # match CSV
                minute = None
                best_csv = None
                for cm in csv_rows:
                    if match_teams(cm, lm):
                        minute = kickoff_minute_from_csv(cm)
                        best_csv = cm
                        break

                if minute is None:
                    # se non riusciamo a stimare il minuto, salta (coerente con la tua base)
                    if DEBUG_LOG:
                        logger.info("Minuto non stimato per %s vs %s", home, away)
                    continue

                # limitiamo ai primi MINUTE_CUTOFF
                if minute > MINUTE_CUTOFF:
                    # se c'era uno stato “in attesa” chiudilo senza notifica
                    key = lm["id"] or f"{home}|{away}|{league}"
                    st = match_state.get(key)
                    if st and st.waiting_relist:
                        st.waiting_relist = False
                        st.scoring_team = None
                        st.baseline_odds = None
                        st.notified = False
                    continue

                key = lm["id"] or f"{home}|{away}|{league}"
                st = match_state.get(key)
                if st is None:
                    st = GoalState(score_tuple=cur_score, minute=minute)
                    match_state[key] = st

                # 1) rileva gol
                scorer = detect_scorer(st.last_score, cur_score)
                if scorer:
                    st.waiting_relist = True
                    st.scoring_team = scorer
                    st.baseline_odds = None
                    st.notified = False
                    if DEBUG_LOG:
                        logger.info("GOL %s: %s vs %s (%s) %s", league, home, away, score_str, scorer)

                st.last_score = cur_score
                st.last_seen_minute = minute

                # 2) se siamo in attesa di re-listing / baseline / salita
                if st.waiting_relist:
                    if not lm.get("id"):
                        # senza event_id non possiamo interrogare le quote
                        if DEBUG_LOG:
                            logger.info("Niente event_id → impossibile leggere odds per %s vs %s", home, away)
                    else:
                        odds = get_event_odds_1x2(lm["id"])
                        if odds:
                            suspended = odds.get("suspended")
                            # fissa baseline alla prima quota “attiva” dopo il gol
                            if st.baseline_odds is None:
                                if suspended is False or suspended is None:
                                    base = odds["home"] if st.scoring_team == "home" else odds["away"]
                                    if base is not None:
                                        st.baseline_odds = base
                                        if DEBUG_LOG:
                                            logger.info("Baseline %s: %.2f (%s vs %s)", st.scoring_team, base, home, away)

                            # se c'è baseline, controlla una qualsiasi SALITA (> baseline)
                            if st.baseline_odds is not None and not st.notified:
                                current = odds["home"] if st.scoring_team == "home" else odds["away"]
                                if current is not None and current > st.baseline_odds:
                                    msg = (
                                        "⚠️ <b>Quota in SALITA dopo il gol</b>\n\n"
                                        f"🏆 {league}\n"
                                        f"🏟️ <b>{home}</b> vs <b>{away}</b>\n"
                                        f"⏱️ <b>{minute}'</b> | Score: <b>{score_str}</b>\n"
                                        f"⚽ Ha segnato: <b>{home if st.scoring_team=='home' else away}</b>\n"
                                        f"📈 Quota {('1' if st.scoring_team=='home' else '2')} "
                                        f"baseline <b>{st.baseline_odds:.2f}</b> → <b>{current:.2f}</b>"
                                    )
                                    if send_telegram_message(msg):
                                        st.notified = True
                                        st.waiting_relist = False  # chiudi episodio
                                    else:
                                        # anche se fallisce l'invio, evita spam continuo
                                        st.notified = True
                                        st.waiting_relist = False

                            # se non è salita e superiamo il cutoff, chiudi senza notifica
                            if st.waiting_relist and minute >= MINUTE_CUTOFF:
                                st.waiting_relist = False
                                st.scoring_team = None
                                st.baseline_odds = None
                                st.notified = False

                # housekeeping: se match sparisce, lo stato verrà sovrascritto nella prossima iterazione

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            send_telegram_message("⛔ Bot arrestato")
            break
        except Exception as e:
            logger.exception("Errore loop: %s", e)
            time.sleep(10)

# =========================
# Entry
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST]):
        raise SystemExit("Env mancanti: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST")

    logger.info("Start | cutoff=%d' | interval=%ds", MINUTE_CUTOFF, CHECK_INTERVAL)
    main_loop()

if __name__ == "__main__":
    main()
