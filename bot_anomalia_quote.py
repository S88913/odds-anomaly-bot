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
# Env
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.getenv("RAPIDAPI_HOST", "bet365data.p.rapidapi.com")
RAPIDAPI_BASE  = os.getenv("RAPIDAPI_BASE", f"https://{RAPIDAPI_HOST}")

# Live events endpoint (ID + minute/period + score + teams)
RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

# Odds 1X2 endpoint (ADATTA ai tuoi)
# Se l'ID va in query, lascia RAPIDAPI_ODDS_QUERY_KEY=event_id e usa '/event-odds'
# Se l'ID va nel path, metti es. '/events/{event_id}/odds-1x2' e RAPIDAPI_ODDS_QUERY_KEY=""
RAPIDAPI_ODDS_PATH      = os.getenv("RAPIDAPI_ODDS_PATH", "/event-odds")
RAPIDAPI_ODDS_QUERY_KEY = os.getenv("RAPIDAPI_ODDS_QUERY_KEY", "event_id")

# Logica
MINUTE_CUTOFF  = int(os.getenv("MINUTE_CUTOFF", "35"))   # entro il 35'
MIN_RISE       = float(os.getenv("MIN_RISE", "0.04"))    # salto minimo: es. 1.36 -> >= 1.40
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
DEBUG_LOG      = os.getenv("DEBUG_LOG", "0") == "1"

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato per evento
# =========================
class GoalState:
    __slots__ = ("last_score","waiting_relist","scoring_team","baseline","notified","last_period")
    def __init__(self, score=(0,0), period=1):
        self.last_score = score
        self.waiting_relist = False
        self.scoring_team = None     # "home" | "away"
        self.baseline = None         # prima quota attiva post-gol (del team che ha segnato)
        self.notified = False        # una sola notifica per episodio
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
            if DEBUG_LOG: logger.info("Telegram: messaggio inviato")
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

def detect_scorer(prev: tuple[int,int], cur: tuple[int,int]) -> str | None:
    ph, pa = prev; ch, ca = cur
    if (ch, ca) == (ph, pa): return None
    if ch == ph + 1 and ca == pa: return "home"
    if ca == pa + 1 and ch == ph: return "away"
    return None

# =========================
# LIVE: id, teams, score, minute, period
# =========================
def get_live_matches():
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=25)
    if not r or not r.ok: return []

    try: data = r.json() or {}
    except Exception:
        logger.error("live-events non-JSON: %s", r.text[:300]); return []

    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []

    for it in raw:
        # prova più chiavi per id/minuto/periodo
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or it.get("fixtureId") or "")
        league   = (it.get("league") or it.get("CT") or it.get("competition") or "N/A").strip()
        home     = (it.get("home") or it.get("HomeTeam") or it.get("homeTeam") or "").strip()
        away     = (it.get("away") or it.get("AwayTeam") or it.get("awayTeam") or "").strip()
        score    = (it.get("SS") or it.get("score") or "").strip()

        raw_minute = it.get("minute") or it.get("clock") or it.get("time") or it.get("timeElapsed") or it.get("TM") or it.get("T") or it.get("clk")
        minute = None
        try:
            minute = int(str(raw_minute).replace("'", "").replace("’","").strip()) if raw_minute is not None else None
        except: minute = None

        # periodo (1 = primo tempo). Molte API hanno 'period', 'half', 'status', 'phase'
        raw_period = it.get("period") or it.get("half") or it.get("phase") or it.get("status")
        period = 1
        try:
            # se è int/str "1", prendi 1; se "1st Half"/"HT"/"2nd Half" mappalo come serve
            if isinstance(raw_period, (int, float)): period = int(raw_period)
            elif isinstance(raw_period, str):
                s = raw_period.lower()
                if "1" in s or "first" in s: period = 1
                elif "2" in s or "second" in s: period = 2
                elif "ht" in s: period = 1
                else: period = 1
        except:
            period = 1

        if home and away and score is not None:
            events.append({
                "id": event_id, "home": home, "away": away,
                "league": league, "SS": score, "minute": minute, "period": period
            })

    logger.info("API live-events: %d match live", len(events))
    return events

# =========================
# ODDS: 1X2 + suspended
# =========================
def get_event_odds_1x2(event_id: str):
    if not event_id: return None
    params = None
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    if RAPIDAPI_ODDS_QUERY_KEY:
        params = {RAPIDAPI_ODDS_QUERY_KEY: event_id}

    r = http_get(url, headers=HEADERS, params=params, timeout=20)
    if not r or not r.ok: return None
    try: data = r.json() or {}
    except Exception:
        logger.error("odds non-JSON: %s", r.text[:300]); return None

    home = draw = away = None
    suspended = None

    markets = data.get("markets") or data.get("data") or []
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("list") or [markets]

    pick = None
    for m in markets or []:
        key = (str(m.get("key") or m.get("name") or m.get("market") or "")).lower()
        if "1x2" in key or key in ("match_result","full_time_result","ft_result","result"):
            pick = m; break
    if not pick and markets: pick = markets[0]

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
# Core loop
# =========================
def main_loop():
    send_telegram_message("🤖 <b>Bot Quote Jump avviato</b>\nMonitoraggio live entro 35'…")

    while True:
        try:
            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL); continue

            for lm in live:
                eid   = lm.get("id") or ""
                home  = lm.get("home","")
                away  = lm.get("away","")
                league= lm.get("league","N/A")
                score = lm.get("SS") or ""
                minute= lm.get("minute")  # da API live
                period= lm.get("period")  # 1 = primo tempo (se disponibile)

                # serve il minuto o almeno il periodo; vogliamo SOLO 1° tempo e <= 35'
                if minute is not None:
                    if minute > MINUTE_CUTOFF: 
                        # chiudi eventuale episodio pendente
                        st = match_state.get(eid or f"{home}|{away}|{league}")
                        if st and st.waiting_relist:
                            st.waiting_relist = False
                            st.scoring_team = None
                            st.baseline = None
                            st.notified = False
                        continue
                # se l'API non fornisce minute, lasciamo passare ma filtriamo per period==1
                if period and int(period) != 1:
                    continue

                cur_score = parse_score_tuple(score)
                key = eid or f"{home}|{away}|{league}"
                st = match_state.get(key)
                if st is None:
                    st = GoalState(score=cur_score, period=period or 1)
                    match_state[key] = st

                # 1) rileva gol
                scorer = detect_scorer(st.last_score, cur_score)
                if scorer:
                    st.waiting_relist = True
                    st.scoring_team = scorer
                    st.baseline = None
                    st.notified = False
                    if DEBUG_LOG:
                        logger.info("GOL %s: %s vs %s | score=%s | period=%s | minute=%s", league, home, away, score, period, minute)

                st.last_score = cur_score
                st.last_period = period or 1

                # 2) se in attesa di re-listing/baseline/salita
                if st.waiting_relist:
                    if not eid:
                        if DEBUG_LOG:
                            logger.info("Nessun event_id per %s vs %s: impossibile leggere odds", home, away)
                        continue

                    odds = get_event_odds_1x2(eid)
                    if not odds:
                        continue

                    suspended = odds.get("suspended")

                    # fissa baseline alla prima quota attiva dopo il gol (anche se suspended mancante)
                    if st.baseline is None:
                        if suspended is False or suspended is None:
                            base = odds["home"] if st.scoring_team == "home" else odds["away"]
                            if base is not None:
                                st.baseline = base
                                if DEBUG_LOG:
                                    logger.info("Baseline %s: %.2f (%s vs %s)", st.scoring_team, base, home, away)

                    # se c'è baseline, notifica quando la quota SALE di almeno MIN_RISE
                    if st.baseline is not None and not st.notified:
                        current = odds["home"] if st.scoring_team == "home" else odds["away"]
                        if current is not None and current >= st.baseline + MIN_RISE:
                            msg = (
                                "⚠️ <b>Quota in SALITA dopo il gol</b>\n\n"
                                f"🏆 {league}\n"
                                f"🏟️ <b>{home}</b> vs <b>{away}</b>\n"
                                f"⏱️ <b>{minute if minute is not None else '1T'}</b> | Score: <b>{score}</b>\n"
                                f"⚽ Ha segnato: <b>{home if st.scoring_team=='home' else away}</b>\n"
                                f"📈 Quota {('1' if st.scoring_team=='home' else '2')} "
                                f"baseline <b>{st.baseline:.2f}</b> → <b>{current:.2f}</b>"
                            )
                            send_telegram_message(msg)
                            st.notified = True
                            st.waiting_relist = False  # chiudi episodio

                    # se non è salita e abbiamo il minuto > cutoff, chiudi senza notifica
                    if st.waiting_relist and minute is not None and minute > MINUTE_CUTOFF:
                        st.waiting_relist = False
                        st.scoring_team = None
                        st.baseline = None
                        st.notified = False

            if DEBUG_LOG:
                waiting = sum(1 for s in match_state.values() if s.waiting_relist)
                logger.info("Loop: live=%d | in_attesa_relist=%d", len(live), waiting)

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            send_telegram_message("⛔ Bot arrestato")
            break
        except Exception as e:
            logger.exception("Errore loop: %s", e)
            time.sleep(8)

# =========================
# Entry
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST]):
        raise SystemExit("Env mancanti: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST")
    logger.info("Start | cutoff=%d' | min_rise=%.2f | interval=%ds", MINUTE_CUTOFF, MIN_RISE, CHECK_INTERVAL)
    main_loop()

if __name__ == "__main__":
    main()
