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

# LIVE list
RAPIDAPI_EVENTS_PATH   = os.getenv("RAPIDAPI_EVENTS_PATH", "/live-events")
RAPIDAPI_EVENTS_PARAMS = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "sport=soccer")))

# ODDS of one event
RAPIDAPI_ODDS_PATH = os.getenv("RAPIDAPI_ODDS_PATH", "/live-events/{event_id}")

# Business rules
MINUTE_CUTOFF   = int(os.getenv("MINUTE_CUTOFF", "45"))      # primo tempo completo
MIN_RISE        = float(os.getenv("MIN_RISE", "0.05"))       # salita minima significativa
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "8"))
DEBUG_LOG       = os.getenv("DEBUG_LOG", "1") == "1"

# Rate-limit guard
MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "3"))
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "800"))
_last_odds_call_ts_ms   = 0

# Attesa dopo goal prima di cercare quote (secondi)
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "45"))

# Daily 429 cooldown
COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# League filters
LEAGUE_EXCLUDE_KEYWORDS = [kw.strip().lower() for kw in os.getenv(
    "LEAGUE_EXCLUDE_KEYWORDS", "Esoccer,8 mins,Volta,H2H GG,Virtual"
).split(",") if kw.strip()]

HEADERS = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

# =========================
# Stato evento
# =========================
class GoalState:
    __slots__ = ("last_score", "goal_detected_at", "scoring_team", "baseline", 
                 "notified", "tries", "last_log_ts", "baseline_set_at", "max_seen")
    
    def __init__(self, score=(0,0)):
        self.last_score = score
        self.goal_detected_at = None  # timestamp
        self.scoring_team = None      # "home" | "away"
        self.baseline = None          # prima quota dopo goal
        self.notified = False
        self.tries = 0
        self.last_log_ts = 0
        self.baseline_set_at = None
        self.max_seen = None          # massima quota vista

match_state: dict[str, GoalState] = {}
_loop = 0

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
                logger.warning("HTTP 429 per-second su %s", url)
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
        try: 
            return (int(nums[0]), int(nums[1]))
        except: 
            return (0,0)
    return (0,0)

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
    """Supporta decimali, frazionali e americane"""
    if x is None: 
        return None
    if isinstance(x, (int, float)):
        val = float(x)
        # Valida range ragionevole per quote
        return val if 1.01 <= val <= 1000 else None
    
    s = str(x).strip()
    
    # Decimale
    try:
        val = float(s.replace(",", "."))
        return val if 1.01 <= val <= 1000 else None
    except:
        pass
    
    # Frazionale (es. 6/4)
    if "/" in s:
        try:
            a, b = s.split("/", 1)
            a = float(a.strip())
            b = float(b.strip())
            if b != 0:
                val = 1.0 + (a / b)
                return val if 1.01 <= val <= 1000 else None
        except:
            pass
    
    # Americana
    if s.startswith("+") or s.startswith("-"):
        try:
            n = int(s)
            if n > 0:
                val = 1.0 + (n / 100.0)
            else:
                val = 1.0 + (100.0 / abs(n))
            return val if 1.01 <= val <= 1000 else None
        except:
            pass
    
    return None

# =========================
# LIVE events
# =========================
def get_live_matches():
    url = build_url(RAPIDAPI_EVENTS_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_EVENTS_PARAMS, timeout=25)
    if not r or not r.ok: 
        return []

    try:
        data = r.json() or {}
    except Exception:
        logger.error("live-events non-JSON: %s", r.text[:300])
        return []

    # Gestione strutture diverse
    raw = (data.get("data") or {}).get("events") or data.get("events") or []
    events = []

    for it in raw:
        event_id = str(it.get("id") or it.get("event_id") or it.get("EId") or it.get("fixtureId") or "")
        league   = (it.get("league") or it.get("CT") or it.get("competition") or "N/A")
        league   = league.strip() if isinstance(league, str) else str(league)

        # Filtra leghe escluse
        if any(kw in league.lower() for kw in LEAGUE_EXCLUDE_KEYWORDS):
            continue

        home  = (it.get("home") or it.get("HomeTeam") or it.get("homeTeam") or "").strip()
        away  = (it.get("away") or it.get("AwayTeam") or it.get("awayTeam") or "").strip()
        score = (it.get("SS") or it.get("score") or "").strip()

        # Parsing minuto
        raw_minute = (
            it.get("minute") or it.get("minutes") or it.get("timeElapsed") or
            it.get("timer") or it.get("clock") or it.get("Clock") or
            it.get("TM") or it.get("T") or it.get("clk")
        )
        
        minute = None
        if raw_minute is not None:
            try:
                # Rimuovi caratteri non numerici
                minute_str = str(raw_minute).replace("'", "").replace("'","").replace("+","").strip()
                # Estrai primo numero
                nums = re.findall(r"\d+", minute_str)
                if nums:
                    minute = int(nums[0])
            except:
                pass

        if not home or not away or not event_id:
            continue

        events.append({
            "id": event_id,
            "home": home,
            "away": away,
            "league": league,
            "SS": score,
            "minute": minute
        })

    # Deduplica
    unique = {}
    for e in events:
        k = e["id"]
        if k not in unique:
            unique[k] = e
    
    events = list(unique.values())
    logger.info("✅ API live-events: %d match live", len(events))
    return events

# =========================
# ODDS parsing
# =========================
DRAW_TOKENS = {"draw", "x", "tie", "empate", "remis", "pareggio", "d", "égalité"}

def _extract_1x2_from_market(m, home_name: str, away_name: str):
    """Estrae quote 1X2 da un mercato - supporta formato Bet365Data"""
    suspended = m.get("suspended") or m.get("Suspended") or m.get("SU")
    if isinstance(suspended, str):
        suspended = suspended.lower() in ("true", "1", "yes", "y")

    # Formato Bet365Data: mg -> ma -> pa
    ma_list = m.get("ma") or []
    if ma_list and isinstance(ma_list, list):
        for ma in ma_list:
            pa_list = ma.get("pa") or []
            if not pa_list:
                continue
            
            home_p = away_p = draw_p = None
            
            for sel in pa_list:
                # Prezzo decimale
                price = parse_price_any(sel.get("decimal") or sel.get("OD"))
                if price is None:
                    continue
                
                # Identificatori
                n2 = str(sel.get("N2") or "").strip().upper()
                label = str(sel.get("NA") or sel.get("name") or "").strip()
                lname = norm_name(label)
                
                # Match per N2 (1, X, 2)
                if n2 == "1" and home_p is None:
                    home_p = price
                elif n2 == "X" and draw_p is None:
                    draw_p = price
                elif n2 == "2" and away_p is None:
                    away_p = price
                # Match per nome
                elif lname in DRAW_TOKENS and draw_p is None:
                    draw_p = price
                elif home_p is None and fuzzy_contains(label, home_name):
                    home_p = price
                elif away_p is None and fuzzy_contains(label, away_name):
                    away_p = price
            
            if any(v is not None for v in (home_p, draw_p, away_p)):
                return {"home": home_p, "draw": draw_p, "away": away_p, "suspended": bool(suspended)}

    # Formato standard (outcomes/prices)
    outcomes = m.get("outcomes") or m.get("outcome") or m.get("prices") or m.get("selections") or m.get("runners")

    # Formato dict: {home: 1.8, draw: 3.5, away: 4.2}
    if isinstance(outcomes, dict):
        home = parse_price_any(outcomes.get("home") or outcomes.get("Home") or outcomes.get("1"))
        draw = parse_price_any(outcomes.get("draw") or outcomes.get("Draw") or outcomes.get("x") or outcomes.get("X"))
        away = parse_price_any(outcomes.get("away") or outcomes.get("Away") or outcomes.get("2"))
        
        if any(v is not None for v in (home, draw, away)):
            return {"home": home, "draw": draw, "away": away, "suspended": bool(suspended)}

    # Formato lista
    if isinstance(outcomes, list):
        home_p = away_p = draw_p = None
        
        for sel in outcomes:
            label = str(sel.get("name") or sel.get("selection") or sel.get("label") or "").strip()
            if not label:
                continue
                
            lname = norm_name(label)
            
            # Cerca prezzo
            price = None
            for key in ("price", "odds", "decimal", "Decimal", "oddsDecimal", "odds_eu", "value"):
                if key in sel:
                    price = parse_price_any(sel[key])
                    if price:
                        break
            
            # Pareggio
            if lname in DRAW_TOKENS and draw_p is None:
                draw_p = price
                continue
            
            # Match per nome squadra
            if home_p is None and fuzzy_contains(label, home_name):
                home_p = price
                continue
            if away_p is None and fuzzy_contains(label, away_name):
                away_p = price
                continue
            
            # Fallback simboli
            if home_p is None and lname in {"1", "home", "team1", "casa", "h"}:
                home_p = price
            elif away_p is None and lname in {"2", "away", "team2", "trasferta", "a"}:
                away_p = price

        if any(v is not None for v in (home_p, draw_p, away_p)):
            return {"home": home_p, "draw": draw_p, "away": away_p, "suspended": bool(suspended)}

    return None

def get_event_odds_1x2(event_id: str, home_name: str, away_name: str, debug_first_call=False):
    """Recupera quote 1X2 per un evento"""
    if not event_id:
        return None
        
    url = build_url(RAPIDAPI_ODDS_PATH, event_id=event_id)
    r = http_get(url, headers=HEADERS, timeout=20)
    
    if not r or not r.ok:
        return None

    try:
        data = r.json() or {}
    except Exception:
        logger.error("odds non-JSON per event %s: %s", event_id, r.text[:300])
        return None

    # DEBUG: Stampa struttura completa alla prima chiamata
    if debug_first_call and DEBUG_LOG:
        import json
        logger.info("="*60)
        logger.info("🔍 DEBUG STRUTTURA API per %s vs %s", home_name, away_name)
        logger.info("Event ID: %s", event_id)
        logger.info("URL: %s", url)
        logger.info("-"*60)
        logger.info("Chiavi root: %s", list(data.keys()))
        
        # Mostra primi 2000 caratteri della risposta formattata
        try:
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            logger.info("Risposta JSON (primi 2000 char):\n%s", pretty[:2000])
            if len(pretty) > 2000:
                logger.info("... [troncato, totale %d caratteri]", len(pretty))
        except:
            logger.info("Raw data: %s", str(data)[:2000])
        logger.info("="*60)

    # Naviga struttura - prova vari percorsi
    root = data.get("data") or data
    
    # Cerca markets in vari posti
    markets = (
        root.get("markets") or 
        root.get("Markets") or 
        root.get("odds") or 
        root.get("bookmakers") or
        root.get("oddsdata") or
        []
    )
    
    if isinstance(markets, dict):
        markets = markets.get("markets") or markets.get("list") or markets.get("items") or [markets]

    if not markets:
        # Prova a cercare direttamente nelle chiavi
        if "results" in root:
            results = root["results"]
            if isinstance(results, dict) and "markets" in results:
                markets = results["markets"]

    # Mercati prioritari
    priority_keywords = [
        "1x2", "match result", "full time result", "ft result", 
        "result", "winner", "to win", "match odds", "match winner",
        "resultado final", "3way", "three way", "moneyline", "1 x 2"
    ]
    
    prioritized = []
    others = []
    
    for m in markets or []:
        market_name = str(m.get("key") or m.get("name") or m.get("market") or m.get("title") or m.get("mn") or "").lower()
        
        if any(kw in market_name for kw in priority_keywords):
            prioritized.append(m)
        else:
            others.append(m)

    # Cerca nei mercati prioritari
    for m in prioritized:
        result = _extract_1x2_from_market(m, home_name, away_name)
        if result:
            return result

    # Cerca negli altri mercati
    for m in others:
        result = _extract_1x2_from_market(m, home_name, away_name)
        if result:
            return result

    return None

# =========================
# Main loop
# =========================
def main_loop():
    global _last_daily_429_ts, _last_odds_call_ts_ms, _loop

    while True:
        try:
            # Cooldown daily 429
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    remaining = COOLDOWN_ON_DAILY_429_MIN * 60 - elapsed
                    logger.warning("⏸️ Cooldown 429: attendo %d secondi", remaining)
                    time.sleep(min(CHECK_INTERVAL, remaining))
                    continue
                _last_daily_429_ts = 0

            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            # Diagnostica periodica
            _loop += 1
            if _loop % 20 == 0 and DEBUG_LOG:
                pt_matches = sum(1 for e in live if e.get("minute") and e["minute"] <= 45)
                zero_zero = sum(1 for e in live if parse_score_tuple(e.get("SS")) == (0, 0))
                logger.info("📊 Live: %d | Primo tempo: %d | Score 0-0: %d", len(live), pt_matches, zero_zero)

            odds_calls_this_loop = 0
            now = time.time()

            for lm in live:
                eid = lm["id"]
                home = lm["home"]
                away = lm["away"]
                league = lm["league"]
                score = lm.get("SS", "")
                minute = lm.get("minute")

                # Salta se oltre cutoff
                if minute is not None and minute > MINUTE_CUTOFF:
                    continue

                cur_score = parse_score_tuple(score)
                st = match_state.get(eid)
                
                if st is None:
                    st = GoalState(score=cur_score)
                    match_state[eid] = st

                prev = st.last_score

                # Rileva PRIMO vantaggio: 0-0 → 1-0 o 0-1
                first_lead = (prev == (0, 0) and (cur_score == (1, 0) or cur_score == (0, 1)))
                
                if first_lead and st.goal_detected_at is None:
                    scorer = "home" if cur_score == (1, 0) else "away"
                    st.goal_detected_at = now
                    st.scoring_team = scorer
                    st.baseline = None
                    st.notified = False
                    st.tries = 0
                    st.baseline_set_at = None
                    st.max_seen = None
                    
                    logger.info("⚽ GOAL! %s vs %s | 0-0 → %d-%d | min=%s | %s", 
                                home, away, cur_score[0], cur_score[1], 
                                str(minute) if minute else "?", league)

                st.last_score = cur_score

                # Processa solo eventi con goal rilevato e non ancora notificati
                if st.goal_detected_at is None or st.notified:
                    continue

                # Attendi dopo il goal prima di cercare quote
                elapsed_after_goal = now - st.goal_detected_at
                if elapsed_after_goal < WAIT_AFTER_GOAL_SEC:
                    continue

                # Rate limiting
                now_ms = int(time.time() * 1000)
                if odds_calls_this_loop >= MAX_ODDS_CALLS_PER_LOOP:
                    continue
                if (now_ms - _last_odds_call_ts_ms) < ODDS_CALL_MIN_GAP_MS:
                    continue

                # Leggi quote
                odds = get_event_odds_1x2(eid, home, away, debug_first_call=(st.tries == 1))
                _last_odds_call_ts_ms = int(time.time() * 1000)
                odds_calls_this_loop += 1
                st.tries += 1

                if not odds:
                    if DEBUG_LOG and (now - st.last_log_ts) > 30:
                        logger.info("⏳ Attendo quote 1X2: %s vs %s (tentativo %d)", home, away, st.tries)
                        st.last_log_ts = now
                    
                    # Dopo molti tentativi, abbandona
                    if st.tries > 40:
                        logger.warning("❌ Quote non disponibili dopo %d tentativi: %s vs %s", st.tries, home, away)
                        st.notified = True  # Ferma monitoraggio
                    continue

                # Verifica sospensione
                if odds.get("suspended"):
                    if DEBUG_LOG and (now - st.last_log_ts) > 30:
                        logger.info("⏸️ Mercato sospeso: %s vs %s", home, away)
                        st.last_log_ts = now
                    continue

                # Imposta baseline alla prima quota valida
                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                
                if scorer_price is None:
                    continue

                if st.baseline is None:
                    st.baseline = scorer_price
                    st.max_seen = scorer_price
                    st.baseline_set_at = now
                    logger.info("📌 Baseline impostata: %s vs %s | Quota %s = %.2f", 
                                home, away, st.scoring_team.upper(), st.baseline)
                    continue

                # Aggiorna massimo visto
                if scorer_price > st.max_seen:
                    st.max_seen = scorer_price

                # Calcola variazione
                delta = scorer_price - st.baseline
                
                # Log periodico
                if DEBUG_LOG and (now - st.last_log_ts) > 25:
                    logger.info("📈 Monitor: %s vs %s | Base=%.2f Curr=%.2f Max=%.2f Delta=%+.2f", 
                                home, away, st.baseline, scorer_price, st.max_seen, delta)
                    st.last_log_ts = now

                # ALERT se quota sale significativamente
                if delta >= MIN_RISE and not st.notified:
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    
                    send_telegram_message(
                        "🚨 <b>QUOTA IN SALITA!</b>\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                        f"⏱️ Minuto: <b>{minute if minute else '1T'}</b>\n\n"
                        f"📈 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"Base: <b>{st.baseline:.2f}</b> → Attuale: <b>{scorer_price:.2f}</b>\n"
                        f"Variazione: <b>+{delta:.2f}</b> ({(delta/st.baseline*100):.1f}%)"
                    )
                    
                    logger.info("✅ NOTIFICA INVIATA: %s vs %s | %.2f → %.2f (+%.2f)", 
                                home, away, st.baseline, scorer_price, delta)
                    
                    st.notified = True

            # Pulizia stati vecchi (oltre 2 ore)
            to_remove = [k for k, v in match_state.items() 
                        if v.goal_detected_at and (now - v.goal_detected_at) > 7200]
            for k in to_remove:
                del match_state[k]
            
            if to_remove and DEBUG_LOG:
                logger.info("🧹 Rimossi %d eventi vecchi", len(to_remove))

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("🛑 Interruzione utente")
            break
        except Exception as e:
            logger.exception("❌ Errore loop: %s", e)
            time.sleep(10)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST]):
        raise SystemExit("❌ Variabili ambiente mancanti: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY, RAPIDAPI_HOST")
    
    logger.info("="*60)
    logger.info("🚀 AVVIO BOT QUOTE JUMP")
    logger.info("="*60)
    logger.info("⚙️  Configurazione:")
    logger.info("   • Minuto cutoff: %d'", MINUTE_CUTOFF)
    logger.info("   • Salita minima: %.2f", MIN_RISE)
    logger.info("   • Intervallo check: %ds", CHECK_INTERVAL)
    logger.info("   • Attesa dopo goal: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Debug: %s", "ON" if DEBUG_LOG else "OFF")
    logger.info("="*60)
    
    send_telegram_message(
        "🤖 <b>Bot Quote Jump ATTIVO</b>\n\n"
        "📋 Monitoraggio:\n"
        "• Match 0-0 che vanno in vantaggio\n"
        "• Solo primo tempo (entro 45')\n"
        f"• Alert se quota sale di almeno {MIN_RISE:.2f}\n\n"
        "✅ Sistema operativo"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
