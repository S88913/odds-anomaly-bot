import os
import time
import re
import unicodedata
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
RAPIDAPI_BASE  = f"https://{RAPIDAPI_HOST}"

RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))
RAPIDAPI_ODDS_PATH = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")

# Business rules
MIN_RISE        = float(os.getenv("MIN_RISE", "0.04"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.90"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "8"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "45"))

# Rate limiting
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "4"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "600"))
_last_odds_call_ts_ms   = 0

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# FILTRI LEGHE - Lista completa di esclusioni
LEAGUE_EXCLUDE_KEYWORDS = [
    "esoccer", "8 mins", "volta", "h2h gg", "virtual", 
    "baller", "30 mins", "20 mins", "10 mins", "12 mins",
    "cyber", "e-football", "esports", "fifa", "pes",
    "simulated", "gtworld", "6 mins", "15 mins"
]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato match
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "scoring_team", 
                 "baseline", "notified", "tries", "rejected_reason")
    
    def __init__(self):
        self.first_seen_at = time.time()
        self.first_seen_score = None
        self.goal_time = None
        self.scoring_team = None
        self.baseline = None
        self.notified = False
        self.tries = 0
        self.rejected_reason = None

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

def is_excluded_league(league_name: str) -> bool:
    """Verifica se la lega è da escludere"""
    league_lower = league_name.lower()
    for keyword in LEAGUE_EXCLUDE_KEYWORDS:
        if keyword.lower() in league_lower:
            logger.info("🚫 Lega esclusa: %s (contiene '%s')", league_name, keyword)
            return True
    return False

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
        league = (it.get("league") or it.get("CT") or "").strip()
        
        # FILTRO LEGA - Escludi tornei speciali/virtuali
        if not league or is_excluded_league(league):
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

def get_odds_1x2(event_id: str, home: str, away: str):
    if not event_id:
        return None
        
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=20)
    
    if not r or not r.ok:
        return None

    try:
        data = r.json() or {}
    except:
        return None

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

    for m in prioritized + others:
        result = extract_1x2(m, home, away)
        if result:
            return result

    return None

def can_call_odds_api():
    global _last_odds_call_ts_ms
    now_ms = int(time.time() * 1000)
    return (now_ms - _last_odds_call_ts_ms) >= ODDS_CALL_MIN_GAP_MS

def mark_odds_api_call():
    global _last_odds_call_ts_ms
    _last_odds_call_ts_ms = int(time.time() * 1000)

# =========================
# Main Loop
# =========================
def main_loop():
    global _last_daily_429_ts, _loop

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

                # STEP 1: Rileva primo goal 0-0 → 1-0/0-1
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    
                    if first_score != (0, 0):
                        if not st.rejected_reason:
                            st.rejected_reason = "non_0-0_iniziale"
                        continue
                    
                    # Primo goal rilevato
                    if cur_score == (1, 0):
                        st.goal_time = now
                        st.scoring_team = "home"
                        logger.info("⚽ GOAL rilevato: %s vs %s (1-0) | %s", home, away, league)
                        continue
                    elif cur_score == (0, 1):
                        st.goal_time = now
                        st.scoring_team = "away"
                        logger.info("⚽ GOAL rilevato: %s vs %s (0-1) | %s", home, away, league)
                        continue
                    else:
                        if cur_score != (0, 0) and not st.rejected_reason:
                            st.rejected_reason = f"score_invalido_{cur_score[0]}-{cur_score[1]}"
                        continue

                # Da qui: goal rilevato

                # STEP 2: Verifica che score sia ancora 1-0 o 0-1
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    if not st.rejected_reason:
                        st.rejected_reason = f"score_cambiato_a_{cur_score[0]}-{cur_score[1]}"
                    continue

                # Già notificato o rifiutato
                if st.notified or st.rejected_reason:
                    continue

                # STEP 3: Attesa post-goal
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # STEP 4: Leggi quote
                if not can_call_odds_api() or odds_calls >= MAX_ODDS_CALLS_PER_LOOP:
                    continue

                odds = get_odds_1x2(eid, home, away)
                mark_odds_api_call()
                odds_calls += 1
                st.tries += 1

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

                # STEP 5: BASELINE - Range quota 1.30-1.90
                if st.baseline is None:
                    if scorer_price < BASELINE_MIN or scorer_price > BASELINE_MAX:
                        st.rejected_reason = f"quota_{scorer_price:.2f}_fuori_range"
                        logger.info("❌ Quota %.2f fuori range [%.2f-%.2f]: %s vs %s", 
                                   scorer_price, BASELINE_MIN, BASELINE_MAX, home, away)
                        continue
                    
                    st.baseline = scorer_price
                    logger.info("✅ Baseline %.2f OK: %s vs %s", scorer_price, home, away)
                    continue

                # STEP 6: Monitora variazione
                delta = scorer_price - st.baseline

                # STEP 7: Salita minima rilevata
                if delta >= MIN_RISE:
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    
                    send_telegram_message(
                        f"💰💎 <b>MAESTRO VAI Cò MUCILL</b> 💵🤑\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{scorer_price:.2f}</b>\n"
                        f"Salita: <b>+{delta:.2f}</b> ({(delta/st.baseline*100):.1f}%)\n\n"
                        f"💎💰 OPPORTUNITÀ RILEVATA 💰💎"
                    )
                    
                    logger.info("✅ ALERT: %s vs %s | %.2f → %.2f (+%.2f)", 
                               home, away, st.baseline, scorer_price, delta)
                    
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
    logger.info("🚀 BOT QUOTE JUMP - SOLO CALCIO VERO")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Solo 0-0 → 1-0/0-1")
    logger.info("   • Min rise: +%.2f", MIN_RISE)
    logger.info("   • Quota range: %.2f - %.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Wait: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot ATTIVO</b> 💰\n\n"
        f"✅ Solo <b>0-0 → 1-0/0-1</b>\n"
        f"✅ Solo <b>CALCIO VERO</b> (no virtual)\n"
        f"✅ Quote <b>{BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}</b>\n"
        f"✅ Rise <b>+{MIN_RISE:.2f}</b>"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
