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

RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

RAPIDAPI_ODDS_PATH      = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")
RAPIDAPI_ODDS_QUERY_KEY = os.getenv("RAPIDAPI_ODDS_QUERY_KEY", "")

MINUTE_CUTOFF  = int(os.getenv("MINUTE_CUTOFF", "35"))
MIN_RISE       = float(os.getenv("MIN_RISE", "0.04"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "5"))
DEBUG_LOG      = os.getenv("DEBUG_LOG", "0") == "1"

LOG_GOAL_DETECTED         = os.getenv("LOG_GOAL_DETECTED", "1") == "1"
TELEGRAM_ON_GOAL_DETECTED = os.getenv("TELEGRAM_ON_GOAL_DETECTED", "1") == "1"

MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "1"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "1200"))
_last_odds_call_ts_ms   = 0

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

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
        self.scoring_team = None
        self.baseline = None
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
            if DEBUG_LOG:
                logger.info("Telegram: messaggio inviato")
            return True
        logger.error("Telegram %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram exception: %s", e)
    return False

def http_get(url, headers=None, params=None, timeout=25):
    global _last_daily_429_ts
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 429:
            txt = (r.text or "").lower()
            if "daily quota" in txt or "exceeded the daily" in txt:
                _last_daily_429_ts = int(time.time())
                logger.error("HTTP 429 DAILY QUOTA su %s", url)
            else:
                logger.error("HTTP 429 per-second su %s", url)
        elif not r.ok:
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

def detect_scorer(prev: tuple[int,int], cur: tuple[int,int]) -> str | None:
    ph, pa = prev; ch, ca = cur
    if (ch, ca) == (ph, pa): return None
    if ch == ph + 1 and ca == pa: return "home"
    if ca == pa + 1 and ch == ph: return "away"
    return None

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
        minute = None
        try:
            raw_minute = it.get("minute") or it.get("minutes")
            if raw_minute:
                minute = int(str(raw_minute).replace("'", "").strip())
        except:
            minute = None

        raw_period = it.get("period") or it.get("half") or 1
        try:
            period = int(raw_period)
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
        key = (str(m.get("key") or m.get("name") or "")).lower()
        if "1x2" in key or key in ("match_result", "full_time_result", "ft_result", "result"):
            pick = m; break
    if not pick and markets:
        pick = markets[0]

    home = away = draw = None
    suspended = None
    if pick:
        suspended = pick.get("suspended") or pick.get("Suspended")
        oc = pick.get("outcomes") or pick.get("runners") or {}
        def to_f(x):
            try: return float(str(x).replace(",", "."))
            except: return None

        if isinstance(oc, dict):
            home = to_f(oc.get("home") or oc.get("1"))
            away = to_f(oc.get("away") or oc.get("2"))
        elif isinstance(oc, list):
            name_map = {}
            for o in oc:
                n = str(o.get("name") or "").lower()
                p = o.get("price") or o.get("odds")
                name_map[n] = to_f(p)
            home = name_map.get("home") or name_map.get("1")
            away = name_map.get("away") or name_map.get("2")

    return {"home": home, "away": away, "draw": draw, "suspended": suspended}

# =========================
# LOOP principale
# =========================
def main_loop():
    send_telegram_message("🤖 <b>Bot Quote Jump avviato</b>\nMonitoraggio live entro 35'…")

    while True:
        try:
            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            for lm in live:
                eid = lm.get("id", "")
                home, away, league = lm["home"], lm["away"], lm["league"]
                score = lm["SS"]
                minute = lm.get("minute")
                period = lm.get("period")

                if period != 1:
                    continue

                cur_score = parse_score_tuple(score)
                key = eid or f"{home}|{away}|{league}"
                st = match_state.get(key)
                if st is None:
                    st = GoalState(score=cur_score, period=period)
                    match_state[key] = st

                scorer = detect_scorer(st.last_score, cur_score)
                if scorer:
                    if LOG_GOAL_DETECTED:
                        logger.info("GOL RILEVATO: %s vs %s | id=%s | %s -> %s | minute=%s",
                                    home, away, eid, st.last_score, cur_score, minute)
                    if TELEGRAM_ON_GOAL_DETECTED:
                        send_telegram_message(
                            f"🟡 Gol rilevato: <b>{home}</b> vs <b>{away}</b>\n"
                            f"Score: <b>{st.last_score[0]}-{st.last_score[1]}</b> → <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                            + (
                                f"⏱️ {minute}' — in attesa quote..."
                                if minute is not None
                                else "⏱️ 1T — in attesa quote..."
                            )
                        )
                    st.waiting_relist = True
                    st.scoring_team = scorer
                    st.baseline = None
                    st.notified = False

                st.last_score = cur_score

                if st.waiting_relist and eid:
                    odds = get_event_odds_1x2(eid)
                    if not odds:
                        continue
                    suspended = odds.get("suspended")

                    if st.baseline is None and not suspended:
                        st.baseline = odds["home"] if st.scoring_team == "home" else odds["away"]
                        if DEBUG_LOG:
                            logger.info("Baseline %s: %.2f", st.scoring_team, st.baseline)

                    if st.baseline and not st.notified:
                        current = odds["home"] if st.scoring_team == "home" else odds["away"]
                        if current and current >= st.baseline + MIN_RISE:
                            send_telegram_message(
                                f"⚠️ <b>Quota in SALITA dopo gol</b>\n\n"
                                f"🏆 {league}\n"
                                f"{home} vs {away}\n"
                                f"⏱️ {minute}' | Score: {cur_score[0]}-{cur_score[1]}\n"
                                f"Quota {st.scoring_team.upper()} {st.baseline:.2f} → {current:.2f}"
                            )
                            st.notified = True
                            st.waiting_relist = False

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Errore loop: %s", e)
            time.sleep(8)

# =========================
# Start
# =========================
def main():
    logger.info("Start | cutoff=%d' | min_rise=%.2f | interval=%ds", MINUTE_CUTOFF, MIN_RISE, CHECK_INTERVAL)
    main_loop()

if __name__ == "__main__":
    main()
