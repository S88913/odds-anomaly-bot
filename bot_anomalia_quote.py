import os
import time
import re
import unicodedata
import logging
import requests
from urllib.parse import parse_qsl
from datetime import datetime

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
RAPIDAPI_BASE  = f"https://{RAPIDAPI_HOST}"

RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))
RAPIDAPI_ODDS_PATH = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")

# Business rules - FILTRI STRETTI
MINUTE_CUTOFF   = int(os.getenv("MINUTE_CUTOFF", "35"))
MIN_RISE        = float(os.getenv("MIN_RISE", "0.04"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.90"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "8"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "45"))
DEBUG_LOG       = os.getenv("DEBUG_LOG", "0") == "1"

# Rate limiting
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "2"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "1000"))
_last_odds_call_ts_ms   = 0

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

LEAGUE_EXCLUDE_KEYWORDS = [kw.strip().lower() for kw in os.getenv(
    "LEAGUE_EXCLUDE_KEYWORDS", "Esoccer,8 mins,Volta,H2H GG,Virtual"
).split(",") if kw.strip()]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato match
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "goal_minute", 
                 "scoring_team", "baseline", "notified", "tries", "rejected_reason",
                 "last_game_minute")
    
    def __init__(self):
        self.first_seen_at = time.time()
        self.first_seen_score = None
        self.goal_time = None
        self.goal_minute = None  # Minuto REALE del goal
        self.scoring_team = None
        self.baseline = None
        self.notified = False
        self.tries = 0
        self.rejected_reason = None
        self.last_game_minute = None

match_state = {}
_loop = 0

# =========================
# Helpers
# =========================
def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        return bool(r and r.ok)
    except Exception as e:
        logger.exception("Telegram error: %s", e)
        return False

def http_get(url, headers=None, params=None, timeout=25):
    global _last_daily_429_ts
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 429:
            if "daily" in (r.text or "").lower():
                _last_daily_429_ts = int(time.time())
                logger.error("HTTP 429 DAILY QUOTA")
        elif not r.ok:
            logger.error("HTTP %s %s", r.status_code, url)
        return r
    except Exception as e:
        logger.error("HTTP error: %s", e)
        return None

def build_url(path: str, **fmt):
    return f"{RAPIDAPI_BASE.rstrip('/')}/{path.format(**fmt).lstrip('/')}"

def parse_score_tuple(ss: str) -> tuple:
    if not ss:
        return (0, 0)
    nums = re.findall(r"\d+", ss)
    if len(nums) >= 2:
        try:
            return (int(nums[0]), int(nums[1]))
        except:
            return (0, 0)
    return (0, 0)

def extract_game_time_from_detail(data: dict) -> int:
    """
    Estrae il minuto reale dal dettaglio evento (endpoint odds).
    Cerca in vari campi comuni del JSON di risposta.
    """
    root = data.get("data") or data
    
    # Campo Tr (time remaining) - formato "45'" o "45"
    tr = root.get("Tr") or root.get("TR") or root.get("tr")
    if tr:
        tr_str = str(tr).strip().replace("'", "").replace("+", "").replace("′", "")
        match = re.search(r"(\d+)", tr_str)
        if match:
            try:
                minute = int(match.group(1))
                if 0 <= minute <= 120:
                    return minute
            except:
                pass
    
    # Campo Eps (elapsed seconds)
    eps = root.get("Eps") or root.get("EPS") or root.get("eps")
    if eps:
        try:
            minute = int(float(eps) // 60)
            if 0 <= minute <= 120:
                return minute
        except:
            pass
    
    # Campo matchTime o time
    match_time = root.get("matchTime") or root.get("time") or root.get("Time")
    if match_time:
        time_str = str(match_time).strip().replace("'", "").replace("′", "")
        match = re.search(r"(\d+)", time_str)
        if match:
            try:
                minute = int(match.group(1))
                if 0 <= minute <= 120:
                    return minute
            except:
                pass
    
    # Campi specifici Bet365
    event_info = root.get("event") or root.get("eventInfo") or {}
    for field in ["Tr", "time", "matchTime", "elapsed"]:
        val = event_info.get(field)
        if val:
            val_str = str(val).strip().replace("'", "").replace("′", "")
            match = re.search(r"(\d+)", val_str)
            if match:
                try:
                    minute = int(match.group(1))
                    if 0 <= minute <= 120:
                        return minute
                except:
                    pass
    
    return None

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))

def norm_name(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"[''`]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def fuzzy_contains(a: str, b: str) -> bool:
    A = set(norm_name(a).split())
    B = set(norm_name(b).split())
    if not A or not B:
        return False
    inter = A & B
    return len([t for t in inter if len(t) >= 3]) >= 1

def parse_price_any(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        val = float(x)
        return val if 1.01 <= val <= 1000 else None
    
    s = str(x).strip()
    try:
        val = float(s.replace(",", "."))
        return val if 1.01 <= val <= 1000 else None
    except:
        pass
    
    if "/" in s:
        try:
            a, b = s.split("/", 1)
            a, b = float(a.strip()), float(b.strip())
            if b != 0:
                val = 1.0 + (a / b)
                return val if 1.01 <= val <= 1000 else None
        except:
            pass
    
    if s.startswith("+") or s.startswith("-"):
        try:
            n = int(s)
            val = 1.0 + (n / 100.0) if n > 0 else 1.0 + (100.0 / abs(n))
            return val if 1.01 <= val <= 1000 else None
        except:
            pass
    
    return None

# =========================
# API
# =========================
def get_live_matches():
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=25)
    if not r or not r.ok:
        return []

    try:
        data = r.json() or {}
    except:
        logger.error("Non-JSON response")
        return []

    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []

    for it in raw:
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or "")
        league = (it.get("league") or it.get("CT") or "N/A").strip()
        
        if any(kw in league.lower() for kw in LEAGUE_EXCLUDE_KEYWORDS):
            continue

        home = (it.get("home") or it.get("HomeTeam") or "").strip()
        away = (it.get("away") or it.get("AwayTeam") or "").strip()
        score = (it.get("SS") or "").strip()

        if not home or not away or not event_id:
            continue

        events.append({
            "id": event_id,
            "home": home,
            "away": away,
            "league": league,
            "score": score
        })

    unique = {}
    for e in events:
        if e["id"] not in unique:
            unique[e["id"]] = e
    
    return list(unique.values())

DRAW_TOKENS = {"draw", "x", "tie", "empate", "remis", "pareggio", "d", "égalité"}

def extract_1x2(m, home_name: str, away_name: str):
    suspended = m.get("suspended") or m.get("SU")
    if isinstance(suspended, str):
        suspended = suspended.lower() in ("true", "1", "yes")

    # Bet365Data format
    ma_list = m.get("ma") or []
    if ma_list:
        for ma in ma_list:
            pa_list = ma.get("pa") or []
            if not pa_list:
                continue
            
            home_p = away_p = draw_p = None
            
            for sel in pa_list:
                price = parse_price_any(sel.get("decimal") or sel.get("OD"))
                if not price:
                    continue
                
                n2 = str(sel.get("N2") or "").strip().upper()
                label = str(sel.get("NA") or "").strip()
                lname = norm_name(label)
                
                if n2 == "1" and home_p is None:
                    home_p = price
                elif n2 == "X" and draw_p is None:
                    draw_p = price
                elif n2 == "2" and away_p is None:
                    away_p = price
                elif lname in DRAW_TOKENS and draw_p is None:
                    draw_p = price
                elif home_p is None and fuzzy_contains(label, home_name):
                    home_p = price
                elif away_p is None and fuzzy_contains(label, away_name):
                    away_p = price
            
            if any(v is not None for v in (home_p, draw_p, away_p)):
                return {"home": home_p, "draw": draw_p, "away": away_p, "suspended": bool(suspended)}

    return None

def get_odds_and_time(event_id: str, home: str, away: str):
    """
    Ottiene sia le quote 1X2 che il minuto di gioco reale.
    Restituisce: (odds_dict, game_minute) o (None, None)
    """
    if not event_id:
        return None, None
        
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=20)
    
    if not r or not r.ok:
        return None, None

    try:
        data = r.json() or {}
    except:
        return None, None

    # Estrai tempo di gioco
    game_minute = extract_game_time_from_detail(data)

    # Estrai quote 1X2
    root = data.get("data") or data
    markets = root.get("mg") or []
    
    priority_kw = ["fulltime result", "match result", "1x2", "ft result"]
    prioritized = []
    others = []
    
    for m in markets or []:
        name = str(m.get("name") or "").lower()
        if any(kw in name for kw in priority_kw):
            prioritized.append(m)
        else:
            others.append(m)

    odds = None
    for m in prioritized + others:
        result = extract_1x2(m, home, away)
        if result:
            odds = result
            break

    return odds, game_minute

# =========================
# Main Loop
# =========================
def main_loop():
    global _last_daily_429_ts, _last_odds_call_ts_ms, _loop

    while True:
        try:
            # Daily 429 cooldown
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    time.sleep(CHECK_INTERVAL)
                    continue
                _last_daily_429_ts = 0

            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            if _loop % 40 == 1:
                logger.info("📊 Monitoring %d live matches", len(live))

            now = time.time()
            odds_calls = 0

            for match in live:
                eid = match["id"]
                home = match["home"]
                away = match["away"]
                league = match["league"]
                score = match["score"]
                cur_score = parse_score_tuple(score)

                # Inizializza stato
                if eid not in match_state:
                    match_state[eid] = MatchState()
                    match_state[eid].first_seen_score = cur_score

                st = match_state[eid]

                # FILTRO 1: Rileva SOLO primo goal 0-0 → 1-0/0-1
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    
                    # Deve partire da 0-0
                    if first_score != (0, 0):
                        if not st.rejected_reason:
                            st.rejected_reason = "non_0-0_iniziale"
                        continue
                    
                    # Primo goal: 0-0 → 1-0 o 0-1
                    if cur_score == (1, 0):
                        st.goal_time = now
                        st.scoring_team = "home"
                        logger.info("⚽ GOAL rilevato: %s vs %s (1-0) | %s", home, away, league)
                    elif cur_score == (0, 1):
                        st.goal_time = now
                        st.scoring_team = "away"
                        logger.info("⚽ GOAL rilevato: %s vs %s (0-1) | %s", home, away, league)
                    else:
                        # Score diverso da 0-0, 1-0, 0-1 = NON primo goal
                        if cur_score != (0, 0) and not st.rejected_reason:
                            st.rejected_reason = f"score_invalido_{cur_score[0]}-{cur_score[1]}"
                        continue

                # Da qui in poi: goal rilevato

                # FILTRO 2: Verifica che score sia ancora 1-0 o 0-1
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    if not st.rejected_reason:
                        st.rejected_reason = f"score_cambiato_a_{cur_score[0]}-{cur_score[1]}"
                    continue

                # Già notificato o rifiutato
                if st.notified or st.rejected_reason:
                    continue

                # Attesa post-goal
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # Rate limiting
                now_ms = int(time.time() * 1000)
                if odds_calls >= MAX_ODDS_CALLS_PER_LOOP:
                    continue
                if (now_ms - _last_odds_call_ts_ms) < ODDS_CALL_MIN_GAP_MS:
                    continue

                # Leggi quote E tempo di gioco
                odds, game_minute = get_odds_and_time(eid, home, away)
                _last_odds_call_ts_ms = now_ms
                odds_calls += 1
                st.tries += 1

                # Salva il minuto di gioco se disponibile
                if game_minute is not None:
                    st.last_game_minute = game_minute
                    # Se non abbiamo ancora salvato il minuto del goal, salvalo ora
                    if st.goal_minute is None:
                        st.goal_minute = game_minute
                        logger.info("📍 Minuto goal registrato: %d' per %s vs %s", game_minute, home, away)

                # Fallback: usa il tempo dall'inizio del monitoraggio
                current_minute = st.last_game_minute if st.last_game_minute is not None else int((now - st.first_seen_at) / 60)

                # FILTRO 3: Verifica che siamo entro MINUTE_CUTOFF
                if current_minute > MINUTE_CUTOFF + 5:
                    st.rejected_reason = f"oltre_{MINUTE_CUTOFF}min"
                    logger.info("⏭️ Match oltre %d': %s vs %s (ora: %d')", 
                               MINUTE_CUTOFF, home, away, current_minute)
                    continue

                if not odds:
                    if st.tries > 30:
                        st.rejected_reason = "no_odds_30_tries"
                        logger.warning("❌ No odds after 30 tries: %s vs %s", home, away)
                    continue

                if odds.get("suspended"):
                    continue

                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                
                if scorer_price is None:
                    continue

                # FILTRO 4: Range quota 1.30 - 1.90
                if st.baseline is None:
                    if scorer_price < BASELINE_MIN or scorer_price > BASELINE_MAX:
                        st.rejected_reason = f"quota_{scorer_price:.2f}_fuori_range"
                        logger.info("❌ Quota %.2f fuori range [%.2f-%.2f]: %s vs %s", 
                                   scorer_price, BASELINE_MIN, BASELINE_MAX, home, away)
                        continue
                    
                    st.baseline = scorer_price
                    goal_min_display = st.goal_minute if st.goal_minute else "?"
                    logger.info("✅ Baseline %.2f OK: %s vs %s (goal: %s', ora: %d')", 
                               scorer_price, home, away, goal_min_display, current_minute)
                    continue

                # Monitora variazione
                delta = scorer_price - st.baseline

                # FILTRO 5: Salita minima
                if delta >= MIN_RISE:
                    # Verifica finale del minuto
                    if current_minute > MINUTE_CUTOFF:
                        st.rejected_reason = f"oltre_{MINUTE_CUTOFF}min_finale"
                        logger.info("⏭️ Quota salita ma oltre %d': %s vs %s", 
                                   MINUTE_CUTOFF, home, away)
                        continue
                    
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    
                    goal_min_text = f"{st.goal_minute}'" if st.goal_minute else "?"
                    
                    send_telegram_message(
                        f"🚨 <b>QUOTA IN SALITA</b>\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                        f"⏱️ Goal al: <b>{goal_min_text}</b> | Ora: <b>{current_minute}'</b>\n\n"
                        f"📈 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"Base: <b>{st.baseline:.2f}</b> → Attuale: <b>{scorer_price:.2f}</b>\n"
                        f"Variazione: <b>+{delta:.2f}</b> ({(delta/st.baseline*100):.1f}%)"
                    )
                    
                    logger.info("✅ ALERT: %s vs %s | %.2f → %.2f (+%.2f) | goal %s ora %d'", 
                               home, away, st.baseline, scorer_price, delta, goal_min_text, current_minute)
                    
                    st.notified = True

            # Pulizia stati vecchi (>2 ore)
            to_remove = [k for k, v in match_state.items() if (now - v.first_seen_at) > 7200]
            for k in to_remove:
                del match_state[k]

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("🛑 Stop")
            break
        except Exception as e:
            logger.exception("Error: %s", e)
            time.sleep(10)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY]):
        raise SystemExit("❌ Missing env vars")
    
    logger.info("="*60)
    logger.info("🚀 BOT QUOTE JUMP - REAL TIME TRACKING v2")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Max minuti: %d'", MINUTE_CUTOFF)
    logger.info("   • Min rise: +%.2f", MIN_RISE)
    logger.info("   • Quota range: %.2f - %.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot ATTIVO - Tempo Reale v2</b>\n\n"
        f"✅ Solo <b>0-0 → 1-0/0-1</b>\n"
        f"✅ Solo entro <b>{MINUTE_CUTOFF}'</b> (tempo reale)\n"
        f"✅ Quote <b>{BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}</b>\n"
        f"✅ Rise <b>+{MIN_RISE:.2f}</b>"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
