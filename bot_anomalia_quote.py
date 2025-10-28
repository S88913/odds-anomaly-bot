import os
import time
import re
import unicodedata
import logging
import requests
from urllib.parse import parse_qsl
from collections import deque

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

# Business rules - OTTIMIZZATI PER VELOCITÀ
MIN_RISE        = float(os.getenv("MIN_RISE", "0.03"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.90"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "20"))  # 20s ottimale

# Baseline sampling - PIÙ VELOCE
BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "6"))  # 6s tra samples

# Rate limiting - PIÙ AGGRESSIVO
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "6"))  # 6 match per loop
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "300"))  # 300ms gap
_last_odds_call_ts_ms   = 0

# Priorità goal recenti (ultimi 2 minuti)
RECENT_GOAL_PRIORITY_SEC = 120

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# API retry
MAX_API_RETRIES = 2  # Ridotto per velocità
API_RETRY_DELAY = 1

# FILTRI LEGHE
LEAGUE_EXCLUDE_KEYWORDS = [
    "esoccer", "8 mins", "volta", "h2h gg", "virtual", 
    "baller", "30 mins", "20 mins", "10 mins", "12 mins",
    "cyber", "e-football", "esports", "fifa", "pes",
    "simulated", "gtworld", "6 mins", "15 mins",
    "torneo regional amateur", "regional amateur"
]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato match
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "scoring_team", 
                 "baseline_samples", "baseline", "last_quote", "notified", 
                 "tries", "last_check", "consecutive_errors", "last_seen_loop")
    
    def __init__(self):
        self.first_seen_at = time.time()
        self.first_seen_score = None
        self.goal_time = None
        self.scoring_team = None
        self.baseline_samples = deque(maxlen=BASELINE_SAMPLES)
        self.baseline = None
        self.last_quote = None
        self.notified = False
        self.tries = 0
        self.last_check = 0
        self.consecutive_errors = 0
        self.last_seen_loop = 0

match_state = {}
_loop = 0

# =========================
# Helpers
# =========================
def send_telegram_message(message: str) -> bool:
    """Invia messaggio Telegram con retry"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("⚠️ Telegram non configurato")
        return False
    
    for attempt in range(2):  # Ridotto a 2 tentativi
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(
                url, 
                data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, 
                timeout=10
            )
            if r and r.ok:
                return True
        except Exception as e:
            logger.warning("Telegram attempt %d error: %s", attempt + 1, e)
        
        if attempt < 1:
            time.sleep(0.5)
    
    return False

def http_get(url, headers=None, params=None, timeout=15, retries=MAX_API_RETRIES):
    """HTTP GET con retry automatico"""
    global _last_daily_429_ts
    
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            
            if r.status_code == 429:
                if "daily" in (r.text or "").lower():
                    _last_daily_429_ts = int(time.time())
                    logger.error("❌ DAILY QUOTA REACHED")
                    return None
                if attempt < retries - 1:
                    time.sleep(API_RETRY_DELAY)
                    continue
            
            if r.ok:
                return r
            
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
                
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
    
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
    """Parsing robusto delle quote"""
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

def create_match_signature(home: str, away: str, league: str) -> str:
    """Crea firma univoca per match"""
    return f"{norm_name(home)}|{norm_name(away)}|{norm_name(league)}"

# =========================
# API
# =========================
def get_live_matches():
    """Recupera match live"""
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=15)
    
    if not r or not r.ok:
        return []

    try:
        data = r.json() or {}
    except Exception as e:
        logger.error("JSON parse error: %s", e)
        return []

    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []
    seen_signatures = set()

    for it in raw:
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or "")
        league = (it.get("league") or it.get("CT") or "").strip()
        
        if not league or is_excluded_league(league):
            continue

        home = (it.get("home") or it.get("HomeTeam") or "").strip()
        away = (it.get("away") or it.get("AwayTeam") or "").strip()
        score = (it.get("SS") or "").strip()

        if not home or not away or not event_id:
            continue

        signature = create_match_signature(home, away, league)
        if signature in seen_signatures:
            continue
        
        seen_signatures.add(signature)

        events.append({
            "id": event_id,
            "home": home,
            "away": away,
            "league": league,
            "score": score,
            "signature": signature
        })

    return events

DRAW_TOKENS = {"draw", "x", "tie", "empate", "remis", "pareggio", "d", "égalité"}

def extract_1x2(m, home_name: str, away_name: str):
    """Estrae quote 1X2"""
    suspended = m.get("suspended") or m.get("SU")
    if isinstance(suspended, str):
        suspended = suspended.lower() in ("true", "1", "yes")

    ma_list = m.get("ma") or []
    if not ma_list:
        return None
    
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
    """Recupera quote 1X2"""
    if not event_id:
        return None
        
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=15)
    
    if not r or not r.ok:
        return None

    try:
        data = r.json() or {}
    except:
        return None

    root = data.get("data") or data
    markets = root.get("mg") or []
    
    priority_kw = ["fulltime result", "match result", "1x2", "ft result", "risultato finale"]
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
# Main Loop - OTTIMIZZATO PER VELOCITÀ
# =========================
def main_loop():
    global _last_daily_429_ts, _loop

    while True:
        try:
            # Controllo daily quota
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    remaining = (COOLDOWN_ON_DAILY_429_MIN * 60 - elapsed) // 60
                    if _loop % 20 == 0:
                        logger.info("⏳ Cooldown: %d min", remaining)
                    time.sleep(CHECK_INTERVAL)
                    continue
                _last_daily_429_ts = 0
                logger.info("✅ Cooldown terminato")

            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            
            # Segna match presenti
            current_match_ids = {m["id"] for m in live}
            for eid in current_match_ids:
                if eid in match_state:
                    match_state[eid].last_seen_loop = _loop

            if _loop % 30 == 1:
                logger.info("📊 %d live | %d monitored", len(live), len(match_state))

            now = time.time()
            odds_calls = 0

            # PRIORITÀ: Match con goal recente (ultimi 2 minuti)
            prioritized_matches = []
            other_matches = []
            
            for match in live:
                eid = match["id"]
                if eid in match_state:
                    st = match_state[eid]
                    if st.goal_time and (now - st.goal_time) < RECENT_GOAL_PRIORITY_SEC:
                        prioritized_matches.append(match)
                        continue
                other_matches.append(match)
            
            # Processa prima i prioritari, poi gli altri
            all_matches = prioritized_matches + other_matches

            for match in all_matches:
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
                    match_state[eid].last_seen_loop = _loop

                st = match_state[eid]
                st.last_seen_loop = _loop

                # STEP 1: Rileva goal
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    
                    if first_score != (0, 0):
                        continue
                    
                    if cur_score == (1, 0):
                        st.goal_time = now
                        st.scoring_team = "home"
                        logger.info("⚽ GOAL: %s vs %s (1-0) | %s", home, away, league)
                        continue
                    elif cur_score == (0, 1):
                        st.goal_time = now
                        st.scoring_team = "away"
                        logger.info("⚽ GOAL: %s vs %s (0-1) | %s", home, away, league)
                        continue
                    else:
                        continue

                # STEP 2: Verifica score
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    continue

                if st.notified:
                    continue

                # STEP 3: Attesa post-goal (RIDOTTA A 15s!)
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # STEP 4: Throttling
                if not can_call_odds_api() or odds_calls >= MAX_ODDS_CALLS_PER_LOOP:
                    continue

                # Throttling per match (RIDOTTO A 5s!)
                if now - st.last_check < BASELINE_SAMPLE_INTERVAL:
                    continue

                odds = get_odds_1x2(eid, home, away)
                mark_odds_api_call()
                odds_calls += 1
                st.tries += 1
                st.last_check = now

                if not odds:
                    st.consecutive_errors += 1
                    if st.consecutive_errors > 8:
                        logger.warning("⚠️ Skip %s vs %s (errori)", home, away)
                        st.notified = True
                    continue

                st.consecutive_errors = 0

                if odds.get("suspended"):
                    continue

                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                
                if scorer_price is None:
                    continue

                # STEP 5: BASELINE (2 campioni invece di 3!)
                if st.baseline is None:
                    if scorer_price < BASELINE_MIN or scorer_price > BASELINE_MAX:
                        logger.info("❌ %.2f fuori range: %s vs %s", scorer_price, home, away)
                        st.notified = True
                        continue
                    
                    st.baseline_samples.append(scorer_price)
                    
                    if len(st.baseline_samples) >= BASELINE_SAMPLES:
                        st.baseline = min(st.baseline_samples)
                        logger.info("✅ Baseline %.2f: %s vs %s", st.baseline, home, away)
                    else:
                        logger.info("📊 Sample %d/%d: %.2f | %s vs %s", 
                                   len(st.baseline_samples), BASELINE_SAMPLES, scorer_price, home, away)
                    
                    st.last_quote = scorer_price
                    continue

                # STEP 6: Monitora
                delta = scorer_price - st.baseline
                st.last_quote = scorer_price

                # Log variazioni anche se non trigger alert
                if delta >= MIN_RISE * 0.7:  # Log se vicino
                    logger.info("📈 %s vs %s: %.2f (base %.2f, Δ+%.3f)", 
                               home, away, scorer_price, st.baseline, delta)

                # STEP 7: Alert (soglia 0.03!)
                if delta >= MIN_RISE:
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    pct = (delta / st.baseline * 100)
                    
                    msg = (
                        f"💰💎 <b>QUOTE JUMP</b> 💎💰\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 <b>{cur_score[0]}-{cur_score[1]}</b>\n\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{scorer_price:.2f}</b>\n"
                        f"📈 <b>+{delta:.2f}</b> (+{pct:.1f}%)\n\n"
                        f"⚡ <b>VAI!</b> ⚡"
                    )
                    
                    if send_telegram_message(msg):
                        logger.info("✅ ALERT: %s vs %s | %.2f→%.2f (+%.2f)", 
                                   home, away, st.baseline, scorer_price, delta)
                    
                    st.notified = True

            # Pulizia aggressiva
            to_remove = []
            for k, v in match_state.items():
                age = now - v.first_seen_at
                loops_ago = _loop - v.last_seen_loop
                
                if loops_ago > 2 or age > 7200:
                    to_remove.append(k)
            
            if to_remove:
                for k in to_remove:
                    del match_state[k]

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("🛑 Stop")
            break
        except Exception as e:
            logger.exception("❌ Error: %s", e)
            time.sleep(5)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY]):
        raise SystemExit("❌ Variabili mancanti")
    
    logger.info("="*60)
    logger.info("🚀 BOT QUOTE JUMP - FAST VERSION")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Min rise: +%.2f", MIN_RISE)
    logger.info("   • Range: %.2f-%.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Wait goal: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("   • Samples: %d (ogni %ds)", BASELINE_SAMPLES, BASELINE_SAMPLE_INTERVAL)
    logger.info("   • Max calls: %d/loop", MAX_ODDS_CALLS_PER_LOOP)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot FAST V2</b> ⚡\n\n"
        f"✅ 0-0 → 1-0/0-1\n"
        f"✅ Quote {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"✅ Rise <b>+{MIN_RISE:.2f}</b>\n"
        f"⚡ Wait <b>{WAIT_AFTER_GOAL_SEC}s</b> | {BASELINE_SAMPLES} samples ogni {BASELINE_SAMPLE_INTERVAL}s\n"
        f"⚡ Max {MAX_ODDS_CALLS_PER_LOOP} calls/loop\n\n"
        f"🔍 Monitoraggio attivo!"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
