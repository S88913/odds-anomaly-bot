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

# LIVE: elenco eventi calcio
RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

# ODDS: mercati di UN evento (ID nel PATH!) per bet365data
# es: GET https://bet365data.p.rapidapi.com/live-events/{event_id}
RAPIDAPI_ODDS_PATH      = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")
RAPIDAPI_ODDS_QUERY_KEY = os.getenv("RAPIDAPI_ODDS_QUERY_KEY", "")  # lasciare vuoto

# Logica
MINUTE_CUTOFF   = int(os.getenv("MINUTE_CUTOFF", "35"))    # solo entro 35'
MIN_RISE        = float(os.getenv("MIN_RISE", "0.04"))     # 1.36 -> >= 1.40 con 0.04
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "5"))
REQUIRE_MINUTE  = os.getenv("REQUIRE_MINUTE", "0") == "1"  # 0 = fallback se minute manca
DEBUG_LOG       = os.getenv("DEBUG_LOG", "1") == "1"

# Gialli (diagnostica) — meglio 0 in produzione
LOG_GOAL_DETECTED         = os.getenv("LOG_GOAL_DETECTED", "0") == "1"
TELEGRAM_ON_GOAL_DETECTED = os.getenv("TELEGRAM_ON_GOAL_DETECTED", "0") == "1"

# Rate limit (per-second)
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "1"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "1200"))
_last_odds_call_ts_ms   = 0

# Daily 429 cooldown (safety)
COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# Filtri
LEAGUE_EXCLUDE_KEYWORDS = [kw.strip().lower() for kw in os.getenv(
    "LEAGUE_EXCLUDE_KEYWORDS", "Esoccer,8 mins,Volta,H2H GG"
).split(",") if kw.strip()]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato evento
# =========================
class GoalState:
    __slots__ = ("last_score","waiting_relist","scoring_team","baseline","notified","tries","last_log_ts")
    def __init__(self, score=(0,0)):
        self.last_score = score
        self.waiting_relist = False
        self.scoring_team = None   # "home" | "away"
        self.baseline = None
        self.notified = False
        self.tries = 0             # quante volte ho letto i mercati in attesa della salita
        self.last_log_ts = 0       # per non spammare i log

match_state: dict[str, GoalState] = {}
_loop = 0  # diagnostica snapshot

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
        return bool(r and r.ok)
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
    nums = re.findall(r"\d+", ss)
    if len(nums) >= 2:
        try: return (int(nums[0]), int(nums[1]))
        except: return (0,0)
    return (0,0)

def detect_scorer(prev: tuple[int,int], cur: tuple[int,int]) -> str | None:
    ph, pa = prev; ch, ca = cur
    if (ch, ca) == (ph, pa): return None
    if ch == ph + 1 and ca == pa: return "home"
    if ca == pa + 1 and ch == ph: return "away"
    return None

def safe_int_minute(raw_minute):
    try:
        return int(str(raw_minute).replace("'", "").replace("’","").strip())
    except:
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
        logger.error("live-events non-JSON: %s", r.text[:300]); return []

    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []

    for it in raw:
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or it.get("fixtureId") or "")
        league   = (it.get("league") or it.get("CT") or it.get("competition") or "N/A")
        league   = league.strip() if isinstance(league, str) else str(league)

        # Escludi rumorosi
        if any(kw in league.lower() for kw in LEAGUE_EXCLUDE_KEYWORDS):
            continue

        home  = (it.get("home") or it.get("HomeTeam") or it.get("homeTeam") or "").strip()
        away  = (it.get("away") or it.get("AwayTeam") or it.get("awayTeam") or "").strip()
        score = (it.get("SS") or it.get("score") or "").strip()

        # minute (se il provider non lo manda, resta None)
        raw_minute = (
            it.get("minute") or it.get("minutes") or it.get("timeElapsed") or
            it.get("timer") or it.get("clock") or it.get("Clock") or
            it.get("TM") or it.get("T") or it.get("clk")
        )
        minute = safe_int_minute(raw_minute) if raw_minute is not None else None

        # molti provider sbagliano "period": non lo usiamo per il trigger
        events.append({
            "id": event_id, "home": home, "away": away,
            "league": league, "SS": score, "minute": minute
        })

    # de-dup (alcuni listati hanno eventi duplicati)
    unique = {}
    for e in events:
        k = e.get("id") or f"{e.get('home','')}|{e.get('away','')}|{e.get('league','')}"
        if k not in unique:
            unique[k] = e
    events = list(unique.values())

    logger.info("API live-events: %d match live", len(events))
    return events

# =========================
# ODDS 1X2 (parser robusto)
# =========================
def _extract_1x2_from_market(m):
    """
    Prova a ricavare home/draw/away/suspended da un singolo 'market' in vari formati.
    """
    name = (str(m.get("key") or m.get("name") or m.get("market") or "")).lower()
    suspended = m.get("suspended")
    if suspended is None:
        suspended = m.get("Suspended")
    if isinstance(suspended, str):
        suspended = suspended.lower() in ("true","1","yes","y")

    # Dizionario outcomes
    oc = m.get("outcomes") or m.get("outcome") or m.get("prices") or m.get("selections")
    if isinstance(oc, dict):
        def to_f(x):
            try: return float(str(x).replace(",", "."))
            except: return None
        home = to_f(oc.get("home") or oc.get("1") or oc.get("team1") or oc.get("home_win") or oc.get("h"))
        draw = to_f(oc.get("draw") or oc.get("x") or oc.get("d"))
        away = to_f(oc.get("away") or oc.get("2") or oc.get("team2") or oc.get("away_win") or oc.get("a"))
        if home or away or draw:
            return {"home": home, "draw": draw, "away": away, "suspended": suspended}

    # Lista di selections/runners
    if isinstance(oc, list):
        name_map = {}
        for o in oc:
            n = (str(o.get("name") or o.get("selection") or o.get("label") or "")).lower()
            price = o.get("price") or o.get("odds") or o.get("decimal") or o.get("Decimal") \
                    or o.get("oddsDecimal") or o.get("odds_eu") or o.get("value")
            try:
                val = float(str(price).replace(",", "."))
            except:
                val = None
            if n:
                name_map[n] = val
        # tante possibili etichette
        home = name_map.get("home") or name_map.get("1") or name_map.get("team1") \
               or name_map.get("home win") or name_map.get("1 (home)") or name_map.get("match home")
        draw = name_map.get("draw") or name_map.get("x") or name_map.get("tie") or name_map.get("pareggio")
        away = name_map.get("away") or name_map.get("2") or name_map.get("team2") \
               or name_map.get("away win") or name_map.get("2 (away)") or name_map.get("match away")
        if home or away or draw:
            return {"home": home, "draw": draw, "away": away, "suspended": suspended}

    return None

def get_event_odds_1x2(event_id: str):
    if not event_id: return None
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=20)
    if not r or not r.ok: return None

    try:
        data = r.json() or {}
    except Exception:
        logger.error("odds non-JSON: %s", r.text[:300]); return None

    root = data.get("data") or data
    markets = root.get("markets") or root.get("Markets") or root.get("odds") or []
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("list") or [markets]

    # 1) cerca un mercato 1X2 per nome
    pick = None
    for m in markets or []:
        nm = (str(m.get("key") or m.get("name") or m.get("market") or "")).lower()
        if "1x2" in nm or "match_result" in nm or "full_time_result" in nm or "ft_result" in nm or nm == "result":
            pick = m; break

    # 2) se non trovato, prova a estrarre 1X2 dall'intera lista
    if pick:
        out = _extract_1x2_from_market(pick)
        if out: return out
    for m in markets or []:
        out = _extract_1x2_from_market(m)
        if out: return out

    # 3) non trovato
    return None

# =========================
# Main loop
# =========================
def main_loop():
    send_telegram_message("🤖 Bot attivo: 0–0 → vantaggio entro 35' con quota in salita.")
    global _last_daily_429_ts, _last_odds_call_ts_ms, _loop

    while True:
        try:
            # cooldown daily
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    time.sleep(min(CHECK_INTERVAL, COOLDOWN_ON_DAILY_429_MIN * 60 - elapsed))
                    continue
                _last_daily_429_ts = 0

            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL); continue

            # snapshot diagnostico ogni 30 giri
            _loop += 1
            if _loop % 30 == 0 and DEBUG_LOG:
                tot = len(live)
                with_min = sum(1 for e in live if e.get("minute") is not None)
                le35 = sum(1 for e in live if (e.get("minute") is not None and e["minute"] <= MINUTE_CUTOFF))
                ss00 = lambda s: re.sub(r"\D", "", str(s or "")) == "00"
                zerozero = sum(1 for e in live if ss00(e.get("SS")))
                logger.info("DBG snapshot: live=%d | minute!=None=%d | minute<=%d=%d | score 0-0=%d",
                            tot, with_min, MINUTE_CUTOFF, le35, zerozero)

            odds_calls_this_loop = 0

            for lm in live:
                eid    = lm.get("id") or ""
                home   = lm.get("home","")
                away   = lm.get("away","")
                league = lm.get("league","N/A")
                score  = lm.get("SS") or ""
                minute = lm.get("minute")

                # filtro tempo
                if REQUIRE_MINUTE:
                    if minute is None or minute > MINUTE_CUTOFF:
                        continue
                else:
                    if minute is not None and minute > MINUTE_CUTOFF:
                        continue
                    # se minute è None, accettiamo (fallback) pur di non perdere il 1T

                cur_score = parse_score_tuple(score)
                key = eid or f"{home}|{away}|{league}"
                st = match_state.get(key)
                if st is None:
                    st = GoalState(score=cur_score)
                    match_state[key] = st

                prev = st.last_score

                # solo PRIMO vantaggio 0-0 -> 1-0 o 0-1
                first_lead = (prev == (0,0) and (cur_score == (1,0) or cur_score == (0,1)))
                if first_lead:
                    scorer = "home" if cur_score == (1,0) else "away"

                    if LOG_GOAL_DETECTED:
                        logger.info("PRIMO VANTAGGIO: %s vs %s | %s -> %s | min=%s | %s",
                                    home, away, prev, cur_score, str(minute), league)
                    if TELEGRAM_ON_GOAL_DETECTED:
                        send_telegram_message(
                            f"🟡 Primo vantaggio: <b>{home}</b> vs <b>{away}</b>\n"
                            f"🏆 {league}\n"
                            f"Score: <b>{prev[0]}-{prev[1]}</b> -> <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                            f"⏱️ {str(minute) if minute is not None else '1T'} — attendo quote…"
                        )

                    st.waiting_relist = True
                    st.scoring_team = scorer
                    st.baseline = None
                    st.notified = False
                    st.tries = 0

                st.last_score = cur_score

                # Leggi quote solo se stiamo aspettando
                if not st.waiting_relist or not eid:
                    continue

                # throttle per-secondo
                now_ms = int(time.time() * 1000)
                if odds_calls_this_loop >= MAX_ODDS_CALLS_PER_LOOP or (now_ms - _last_odds_call_ts_ms) < ODDS_CALL_MIN_GAP_MS:
                    continue

                odds = get_event_odds_1x2(eid)
                _last_odds_call_ts_ms = int(time.time() * 1000)
                odds_calls_this_loop += 1
                st.tries += 1

                if not odds:
                    # log leggero ogni ~20s
                    if DEBUG_LOG and time.time() - st.last_log_ts > 20:
                        logger.info("No 1X2 market yet: %s vs %s (try=%d)", home, away, st.tries)
                        st.last_log_ts = time.time()
                    continue

                suspended = odds.get("suspended")

                # baseline alla prima quota attiva del team che ha segnato
                if st.baseline is None:
                    if suspended is True:
                        if DEBUG_LOG and time.time() - st.last_log_ts > 20:
                            logger.info("Market suspended: %s vs %s (try=%d)", home, away, st.tries)
                            st.last_log_ts = time.time()
                    else:
                        base = odds["home"] if st.scoring_team == "home" else odds["away"]
                        if base is not None:
                            st.baseline = base
                            if DEBUG_LOG:
                                logger.info("Baseline %s %s vs %s: %.2f", st.scoring_team, home, away, base)
                        else:
                            if DEBUG_LOG and time.time() - st.last_log_ts > 20:
                                logger.info("No price for %s yet (market present): %s vs %s", st.scoring_team, home, away)
                                st.last_log_ts = time.time()
                    continue  # aspetta prossimi giri per verificare salita

                # se c'è baseline, verifica salita
                current = odds["home"] if st.scoring_team == "home" else odds["away"]
                if current is None:
                    continue

                delta = current - st.baseline
                # log diagnostico non invadente
                if DEBUG_LOG and time.time() - st.last_log_ts > 20:
                    logger.info("Track %s vs %s | base=%.2f cur=%.2f delta=%+.2f (try=%d)",
                                home, away, st.baseline, current, delta, st.tries)
                    st.last_log_ts = time.time()

                if delta >= MIN_RISE:
                    team_label = "1" if st.scoring_team == "home" else "2"
                    send_telegram_message(
                        "⚠️ <b>Quota in SALITA dopo il primo vantaggio</b>\n\n"
                        f"🏆 {league}\n"
                        f"🏟️ <b>{home}</b> vs <b>{away}</b>\n"
                        f"⏱️ {str(minute) if minute is not None else '1T'} | Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                        f"📈 Quota {team_label}: <b>{st.baseline:.2f}</b> → <b>{current:.2f}</b> "
                        f"(<b>{delta:+.2f}</b>)"
                    )
                    st.notified = True
                    st.waiting_relist = False  # chiudi episodio

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.exception("Errore loop: %s", e)
            time.sleep(6)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST]):
        raise SystemExit("Env mancanti: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST")
    logger.info("Start | cutoff=%d' | min_rise=%.2f | interval=%ds | require_minute=%s",
                MINUTE_CUTOFF, MIN_RISE, CHECK_INTERVAL, str(REQUIRE_MINUTE))
    send_telegram_message(
        "🤖 Bot attivo.\n"
        "Segnalo solo 0-0 → vantaggio entro 35' con quota in salita."
    )
    main_loop()

if __name__ == "__main__":
    main()
