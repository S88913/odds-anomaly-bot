import os
import time
import re
import logging
import requests
from urllib.parse import parse_qsl

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("quote-jump-bot")

# =========================
# Environment
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.getenv("RAPIDAPI_HOST", "bet365data.p.rapidapi.com")
RAPIDAPI_BASE  = os.getenv("RAPIDAPI_BASE", f"https://{RAPIDAPI_HOST}")

# LIVE list
RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

# ODDS of one event (ID in PATH for bet365data)
RAPIDAPI_ODDS_PATH      = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")
RAPIDAPI_ODDS_QUERY_KEY = os.getenv("RAPIDAPI_ODDS_QUERY_KEY", "")  # keep empty for path-id

# Business rules
MINUTE_CUTOFF  = int(os.getenv("MINUTE_CUTOFF", "35"))      # only first 35'
MIN_RISE       = float(os.getenv("MIN_RISE", "0.01"))       # minimal rise (1.36 -> >= 1.37 with 0.01)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "5"))
DEBUG_LOG      = os.getenv("DEBUG_LOG", "0") == "1"

# Diagnostics (default OFF to evitare rumore)
LOG_GOAL_DETECTED         = os.getenv("LOG_GOAL_DETECTED", "0") == "1"
TELEGRAM_ON_GOAL_DETECTED = os.getenv("TELEGRAM_ON_GOAL_DETECTED", "0") == "1"

# Per-second rate limit guard
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "1"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "1200"))
_last_odds_call_ts_ms   = 0

# League filters
LEAGUE_EXCLUDE_KEYWORDS = [kw.strip().lower() for kw in os.getenv(
    "LEAGUE_EXCLUDE_KEYWORDS", "Esoccer,8 mins,Volta,H2H GG"
).split(",") if kw.strip()]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato evento
# =========================
class GoalState:
    __slots__ = ("last_score","waiting_relist","scoring_team","baseline","notified","last_period")
    def __init__(self, score=(0,0), period=1):
        self.last_score = score
        self.waiting_relist = False
        self.scoring_team = None     # "home" | "away"
        self.baseline = None         # first post-goal odds for scoring team
        self.notified = False
        self.last_period = period

match_state: dict[str, GoalState] = {}

# =========================
# Helpers
# =========================
def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_TOKEN/CHAT_ID mancanti.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        if r.ok:
            return True
        logger.error("Telegram %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram exception: %s", e)
    return False

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

def parse_score_tuple(ss: str) -> tuple[int,int]:
    if not ss: return (0,0)
    parts = re.findall(r"\d+", ss)
    if len(parts) >= 2:
        try: return (int(parts[0]), int(parts[1]))
        except: return (0,0)
    return (0,0)

# =========================
# LIVE events
# =========================
def get_live_matches():
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=25)
    if not r or not r.ok: return []

    try:
        data = r.json() or {}
    except Exception:
        logger.error("live-events non-JSON: %s", r.text[:300])
        return []

    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []
    for it in raw:
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or "")
        league   = (it.get("league") or it.get("CT") or "N/A")
        league   = league.strip() if isinstance(league, str) else str(league)
        if any(kw in league.lower() for kw in LEAGUE_EXCLUDE_KEYWORDS):
            continue

        home = (it.get("home") or it.get("HomeTeam") or "").strip()
        away = (it.get("away") or it.get("AwayTeam") or "").strip()

        score = (it.get("SS") or it.get("score") or "").strip()

        # minute & period (useful to filter 1st half; if minute missing, we will SKIP)
        minute = None
        try:
            raw_minute = it.get("minute") or it.get("minutes") or it.get("timeElapsed") or it.get("clock")
            if raw_minute is not None:
                minute = int(str(raw_minute).replace("'", "").replace("’","").strip())
        except:
            minute = None

        raw_period = it.get("period") or it.get("half") or it.get("phase") or it.get("status")
        period = 1
        try:
            if isinstance(raw_period, (int, float)): period = int(raw_period)
            elif isinstance(raw_period, str):
                s = raw_period.lower()
                if "2" in s or "second" in s: period = 2
                else: period = 1
        except:
            period = 1

        events.append({
            "id": event_id, "home": home, "away": away,
            "league": league, "SS": score, "minute": minute, "period": period
        })
    logger.info("API live-events: %d match live", len(events))
    return events

# =========================
# ODDS 1X2
# =========================
def get_event_odds_1x2(event_id: str):
    if not event_id: return None
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=20)
    if not r or not r.ok: return None

    try:
        data = r.json() or {}
    except Exception:
        logger.error("odds non-JSON: %s", r.text[:300])
        return None

    root = data.get("data") or data
    markets = root.get("markets") or root.get("Markets") or root.get("odds") or []
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("list") or [markets]

    pick = None
    for m in markets or []:
        key = (str(m.get("key") or m.get("name") or m.get("market") or "")).lower()
        if "1x2" in key or key in ("match_result","full_time_result","ft_result","result"):
            pick = m; break
    if not pick and markets:
        pick = markets[0]

    home = draw = away = None
    suspended = None
    if pick:
        if "suspended" in pick: suspended = bool(pick.get("suspended"))
        elif "Suspended" in pick: suspended = bool(pick.get("Suspended"))

        oc = pick.get("outcomes") or pick.get("runners") or pick.get("outcome") or {}
        def to_f(x):
            try: return float(str(x).replace(",", "."))
            except: return None

        if isinstance(oc, dict):
            home = to_f(oc.get("home") or oc.get("1") or oc.get("Home") or oc.get("team1"))
            draw = to_f(oc.get("draw") or oc.get("X") or oc.get("Draw"))
            away = to_f(oc.get("away") or oc.get("2") or oc.get("Away") or oc.get("team2"))
        elif isinstance(oc, list):
            name_map = {}
            for o in oc:
                name = str(o.get("name") or o.get("selection") or "").lower()
                price = o.get("price") or o.get("odds") or o.get("decimal") or o.get("Decimal")
                name_map[name] = to_f(price)
            home = name_map.get("home") or name_map.get("1") or name_map.get("team1")
            draw = name_map.get("draw") or name_map.get("x")
            away = name_map.get("away") or name_map.get("2") or name_map.get("team2")

    return {"home": home, "draw": draw, "away": away, "suspended": suspended}

# =========================
# LOOP principale
# =========================
def main_loop():
    send_telegram_message("🤖 <b>Bot attivo</b>\nSegnalo quando, nel <b>1° tempo (≤35')</b>, il punteggio passa da 0–0 a vantaggio e la quota della squadra che ha segnato <b>sale</b>.")

    global _last_odds_call_ts_ms

    while True:
        try:
            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            odds_calls_this_loop = 0

            for lm in live:
                eid    = lm.get("id") or ""
                home   = lm.get("home","")
                away   = lm.get("away","")
                league = lm.get("league","N/A")
                score  = lm.get("SS") or ""
                minute = lm.get("minute")  # SE minute è None -> SKIP per evitare sfondoni
                period = lm.get("period", 1)

                # serve minuto e deve essere 1° tempo entro 35'
                if minute is None or minute > MINUTE_CUTOFF or int(period) != 1:
                    continue

                cur_score = parse_score_tuple(score)
                key = eid or f"{home}|{away}|{league}"
                st = match_state.get(key)
                if st is None:
                    st = GoalState(score=cur_score, period=period)
                    match_state[key] = st

                prev = st.last_score
                # SCATTO SOLO SE PRIMO VANTAGGIO: 0-0 -> 1-0 o 0-1
                first_lead = (prev == (0,0) and (cur_score in [(1,0),(0,1)]))
                if first_lead:
                    scorer = "home" if cur_score == (1,0) else "away"

                    if LOG_GOAL_DETECTED:
                        logger.info("PRIMO VANTAGGIO rilevato: %s vs %s | %s -> %s | %d' | league=%s",
                                    home, away, prev, cur_score, minute, league)
                    if TELEGRAM_ON_GOAL_DETECTED:
                        send_telegram_message(
                            f"🟡 Primo vantaggio: <b>{home}</b> vs <b>{away}</b>\n"
                            f"🏆 {league}\n"
                            f"Score: <b>{prev[0]}-{prev[1]}</b> → <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                            f"⏱️ {minute}' — in attesa quote…"
                        )

                    # attiva episodio-gol
                    st.waiting_relist = True
                    st.scoring_team = scorer
                    st.baseline = None
                    st.notified = False

                st.last_score = cur_score
                st.last_period = period

                # Se non abbiamo primo vantaggio, NON sprechiamo chiamate odds
                if not st.waiting_relist or not eid:
                    continue

                # throttle per-second
                now_ms = int(time.time() * 1000)
                if odds_calls_this_loop >= MAX_ODDS_CALLS_PER_LOOP or (now_ms - _last_odds_call_ts_ms) < ODDS_CALL_MIN_GAP_MS:
                    continue

                odds = get_event_odds_1x2(eid)
                _last_odds_call_ts_ms = int(time.time() * 1000)
                odds_calls_this_loop += 1
                if not odds:
                    continue

                suspended = odds.get("suspended")

                # fissa baseline alla prima quota attiva
                if st.baseline is None and (suspended is False or suspended is None):
                    base = odds["home"] if st.scoring_team == "home" else odds["away"]
                    if base is not None:
                        st.baseline = base
                        if DEBUG_LOG:
                            logger.info("Baseline %s %s vs %s: %.2f", st.scoring_team, home, away, base)

                # se c'è baseline, notifica alla prima SALITA >= MIN_RISE
                if st.baseline is not None and not st.notified:
                    current = odds["home"] if st.scoring_team == "home" else odds["away"]
                    if current is not None and current >= st.baseline + MIN_RISE:
                        delta = current - st.baseline
                        send_telegram_message(
                            "⚠️ <b>Quota in SALITA dopo il primo vantaggio</b>\n\n"
                            f"🏆 {league}\n"
                            f"🏟️ <b>{home}</b> vs <b>{away}</b>\n"
                            f"⏱️ <b>{minute}'</b> | Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                            f"⚽ Ha segnato: <b>{home if st.scoring_team=='home' else away}</b>\n"
                            f"📈 Quota {('1' if st.scoring_team=='home' else '2')}: "
                            f"<b>{st.baseline:.2f}</b> → <b>{current:.2f}</b> "
                            f"(+{delta:.2f})"
                        )
                        st.notified = True
                        st.waiting_relist = False  # chiudi episodio

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Errore loop: %s", e)
            time.sleep(8)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST]):
        raise SystemExit("Env mancanti: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST")
    logger.info("Start | cutoff=%d' | min_rise=%.2f | interval=%ds", MINUTE_CUTOFF, MIN_RISE, CHECK_INTERVAL)
    send_telegram_message("🤖 <b>Bot attivo</b> — segnalerò solo <b>0–0 → vantaggio</b> nel <b>1° tempo (≤35')</b> con <b>quota in salita</b>.")
    main_loop()

if __name__ == "__main__":
    main()
