# -*- coding: utf-8 -*-
import os
import time
import re
import unicodedata
import logging
import requests
from urllib.parse import parse_qsl
from collections import deque
import math

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("quote-jump-bot-newapi")

# =========================
# Environment (nomi invariati)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

# Nuovo host di default (ma legge sempre RAPIDAPI_HOST se lo hai settato su Render)
RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.getenv("RAPIDAPI_HOST", "soccer-football-info.p.rapidapi.com")
RAPIDAPI_BASE  = f"https://{RAPIDAPI_HOST}"

# Endpoint/parametri nuova API (nomi ENV invariati: se non li hai, usa i default sotto)
RAPIDAPI_LIVE_PATH    = os.getenv("RAPIDAPI_EVENTS_PATH", "/live/basic")  # vecchia ENV riutilizzata
# parametri query della live (default adatti allo screenshot)
RAPIDAPI_LIVE_PARAMS  = dict(parse_qsl(os.getenv("RAPIDAPI_EVENTS_PARAMS", "l=en_US&f=json&e=no")))
# non serve un path odds separato: le quote sono nel payload dell'evento
RAPIDAPI_ODDS_PATH    = os.getenv("RAPIDAPI_ODDS_PATH", "")  # ignorato, lasciato per compatibilità

# Business rules (invariati)
MIN_RISE        = float(os.getenv("MIN_RISE", "0.03"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.90"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "20"))

BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "6"))

MAX_ODDS_CALLS_PER_LOOP = int(os.getenv("MAX_ODDS_CALLS_PER_LOOP", "6"))  # qui rimane ma non si userà molto
ODDS_CALL_MIN_GAP_MS    = int(os.getenv("ODDS_CALL_MIN_GAP_MS", "300"))
_last_odds_call_ts_ms   = 0

RECENT_GOAL_PRIORITY_SEC = 120
COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

MAX_API_RETRIES = 2
API_RETRY_DELAY = 1

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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("⚠️ Telegram non configurato")
        return False
    for attempt in range(2):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
            if r and r.ok:
                return True
        except Exception as e:
            logger.warning("Telegram attempt %d error: %s", attempt + 1, e)
        if attempt < 1:
            time.sleep(0.5)
    return False

def http_get(url, headers=None, params=None, timeout=15, retries=MAX_API_RETRIES):
    global _last_daily_429_ts
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 429:
                _last_daily_429_ts = int(time.time())
                logger.error("❌ QUOTA 429")
                return None
            if r.ok:
                return r
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
        except Exception:
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
    return f"{norm_name(home)}|{norm_name(away)}|{norm_name(league)}"

# =========================
# Nuovo: parsing timer/minuto
# =========================
def parse_timer(timer: str):
    """
    Accetta formati tipo:
      "27:34"                    -> (period, minute, stoppage_secs=0)
      "45:00+02:30" (45'+2:30)   -> (1H, 47, 150)
      "90:00+00:19" (90'+19s)    -> (2H, 90 o 91, 19)
    Ritorna (period: "1H"/"2H"/"HT"/"FT"/None, minute:int|None, stoppage_secs:int)
    """
    s = (timer or "").strip()
    if not s:
        return (None, None, 0)
    m = re.match(r"^(\d{1,3}):(\d{2})(?:\+(\d{2}):(\d{2}))?$", s)
    if not m:
        if "match finished" in s.lower():
            return ("FT", 90, 0)
        if "half-time" in s.lower():
            return ("HT", 45, 0)
        return (None, None, 0)
    base_min = int(m.group(1))
    base_sec = int(m.group(2))
    plus_sec = 0
    if m.group(3):
        plus_sec = int(m.group(3))*60 + int(m.group(4))
    minute = base_min + (0 if plus_sec == 0 else int(math.ceil(plus_sec/60.0)))
    period = "1H" if base_min < 45 or (base_min == 45 and plus_sec == 0) else "2H"
    if base_min >= 90 and plus_sec == 0:
        period = "2H"
    return (period, minute, plus_sec)

# =========================
# API (nuova)
# =========================
def get_live_matches():
    """Recupera tutti i match live dalla NUOVA API + minuto (se presente) + odds integrate."""
    url = build_url(RAPIDAPI_LIVE_PATH)
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_LIVE_PARAMS, timeout=15)
    if not r or not r.ok:
        return []

    try:
        data = r.json() or {}
    except Exception as e:
        logger.error("JSON parse error: %s", e)
        return []

    # La nuova API può usare "events" o "result"
    raw = data.get("events") or data.get("result") or []
    events = []
    seen_signatures = set()

    for it in raw:
        # nomi
        home = (it.get("home") or (it.get("teamA") or {}).get("name") or "").strip()
        away = (it.get("away") or (it.get("teamB") or {}).get("name") or "").strip()
        league = (it.get("leagueName") or (it.get("championship") or {}).get("name") or "").strip()
        if not home or not away or not league:
            continue
        if is_excluded_league(league):
            continue

        # score
        try:
            hs = int((it.get("homeScore") or (it.get("teamA") or {}).get("score", {}).get("f") or 0) or 0)
            as_ = int((it.get("awayScore") or (it.get("teamB") or {}).get("score", {}).get("f") or 0) or 0)
        except Exception:
            hs, as_ = 0, 0
        score = f"{hs}-{as_}"

        # id
        event_id = str(it.get("eventId") or it.get("id") or "")

        # minuto/periodo
        current = it.get("current") or it.get("timer") or ""
        period, minute, stoppage = parse_timer(current)

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
            "signature": signature,
            "period": period,
            "minute": minute,
            "stoppage": stoppage,
            "raw": it,  # per leggere le odds senza seconda chiamata
        })

    return events

DRAW_TOKENS = {"draw", "x", "tie", "empate", "remis", "pareggio", "égalité", "d"}

def get_odds_1x2_from_event(event_obj):
    """Estrae 1X2 dal campo odds dell'evento (nuova API)."""
    it = event_obj.get("raw") or {}
    odds_groups = it.get("odds") or []
    if not odds_groups:
        return None

    # cerca gruppo 1X2
    target = None
    for g in odds_groups:
        name = (g.get("name") or "").strip().lower()
        if name in ("result", "match result", "1x2", "ft result", "risultato finale"):
            target = g
            break
    if target is None:
        # fallback: primo gruppo con >=2 outcomes
        for g in odds_groups:
            if len(g.get("markets") or []) >= 2:
                target = g
                break
    if target is None:
        return None

    home_p = draw_p = away_p = None
    markets = target.get("markets") or []
    any_show = False

    for m in markets:
        label = (m.get("name") or "").strip()
        status = (m.get("status") or "").lower()
        price = parse_price_any(m.get("rate"))
        if status == "show":
            any_show = True
        if price is None:
            continue

        lname = label.lower()
        if home_p is None and fuzzy_contains(label, event_obj["home"]):
            home_p = price
            continue
        if away_p is None and fuzzy_contains(label, event_obj["away"]):
            away_p = price
            continue
        if draw_p is None and lname in DRAW_TOKENS:
            draw_p = price

    # eventuale fallback sull'ordine (home, draw, away)
    if (home_p is None or away_p is None) and len(markets) >= 3:
        p0 = parse_price_any(markets[0].get("rate"))
        p1 = parse_price_any(markets[1].get("rate"))
        p2 = parse_price_any(markets[2].get("rate"))
        if p0 and p1 and p2 and home_p is None and draw_p is None and away_p is None:
            home_p, draw_p, away_p = p0, p1, p2

    if any(v is not None for v in (home_p, draw_p, away_p)):
        return {"home": home_p, "draw": draw_p, "away": away_p, "suspended": not any_show}
    return None

def can_call_odds_api():
    # mantenuto per compatibilità (qui praticamente non serve)
    now_ms = int(time.time() * 1000)
    return (now_ms - _last_odds_call_ts_ms) >= ODDS_CALL_MIN_GAP_MS

def mark_odds_api_call():
    global _last_odds_call_ts_ms
    _last_odds_call_ts_ms = int(time.time() * 1000)

# =========================
# Main Loop (immutato nella logica, con minuto in notifica)
# =========================
def main_loop():
    global _last_daily_429_ts, _loop

    while True:
        try:
            # Cooldown 429
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    if _loop % 20 == 0:
                        rem = (COOLDOWN_ON_DAILY_429_MIN * 60 - elapsed) // 60
                        logger.info("⏳ Cooldown: %d min", rem)
                    time.sleep(CHECK_INTERVAL)
                    continue
                _last_daily_429_ts = 0
                logger.info("✅ Cooldown terminato")

            live = get_live_matches()
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            current_match_ids = {m["id"] for m in live}
            for eid in current_match_ids:
                if eid in match_state:
                    match_state[eid].last_seen_loop = _loop

            if _loop % 30 == 1:
                logger.info("📊 %d live | %d monitored", len(live), len(match_state))

            now = time.time()
            odds_calls = 0

            # priorità goal recenti
            prioritized, others = [], []
            for match in live:
                eid = match["id"]
                if eid in match_state:
                    st = match_state[eid]
                    if st.goal_time and (now - st.goal_time) < RECENT_GOAL_PRIORITY_SEC:
                        prioritized.append(match); continue
                others.append(match)
            all_matches = prioritized + others

            for match in all_matches:
                eid = match["id"]; home = match["home"]; away = match["away"]
                league = match["league"]; score = match["score"]
                cur_score = parse_score_tuple(score)

                # init stato
                if eid not in match_state:
                    match_state[eid] = MatchState()
                    match_state[eid].first_seen_score = cur_score
                    match_state[eid].last_seen_loop = _loop

                st = match_state[eid]
                st.last_seen_loop = _loop

                # STEP 1: rileva gol di sblocco
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    if first_score != (0, 0):
                        continue
                    if cur_score == (1, 0):
                        st.goal_time = now; st.scoring_team = "home"
                        logger.info("⚽ GOAL: %s vs %s (1-0) | %s", home, away, league)
                        continue
                    elif cur_score == (0, 1):
                        st.goal_time = now; st.scoring_team = "away"
                        logger.info("⚽ GOAL: %s vs %s (0-1) | %s", home, away, league)
                        continue
                    else:
                        continue

                # STEP 2: score atteso
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    continue
                if st.notified:
                    continue

                # STEP 3: attesa post-goal
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # STEP 4: throttling per match
                if not can_call_odds_api() or odds_calls >= MAX_ODDS_CALLS_PER_LOOP:
                    continue
                if now - st.last_check < BASELINE_SAMPLE_INTERVAL:
                    continue

                # Quote 1X2 direttamente dal payload del match (nessuna GET extra)
                odds = get_odds_1x2_from_event(match)
                mark_odds_api_call()
                odds_calls += 1
                st.tries += 1
                st.last_check = now

                if not odds:
                    st.consecutive_errors += 1
                    if st.consecutive_errors > 8:
                        logger.warning("⚠️ Skip %s vs %s (no odds)", home, away)
                        st.notified = True
                    continue

                st.consecutive_errors = 0
                if odds.get("suspended"):
                    continue

                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                if scorer_price is None:
                    continue

                # STEP 5: baseline
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

                # STEP 6: monitora varianza
                delta = scorer_price - st.baseline
                st.last_quote = scorer_price
                if delta >= MIN_RISE * 0.7:
                    logger.info("📈 %s vs %s: %.2f (base %.2f, Δ+%.3f)",
                                home, away, scorer_price, st.baseline, delta)

                # STEP 7: alert
                if delta >= MIN_RISE:
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    pct = (delta / st.baseline * 100)

                    # Minuto preciso dalla live già parsata
                    period = match.get("period")
                    minute = match.get("minute")
                    stoppage = match.get("stoppage") or 0
                    rec_str = ""
                    if stoppage:
                        rec_str = f"+{stoppage//60}'" if stoppage >= 60 else f"+{stoppage}s"
                    time_str = f"\n⏱ <b>{(period or '')} {(str(minute)+'\\'' if minute is not None else '')}{(' ' + rec_str) if rec_str else ''}</b>" if (period or minute is not None) else ""

                    msg = (
                        f"💰💎 <b>QUOTE JUMP</b> 💎💰\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 <b>{cur_score[0]}-{cur_score[1]}</b>{time_str}\n\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{scorer_price:.2f}</b>\n"
                        f"📈 <b>+{delta:.2f}</b> (+{pct:.1f}%)\n\n"
                        f"⚡ <b>VAI!</b> ⚡"
                    )

                    if send_telegram_message(msg):
                        logger.info("✅ ALERT: %s vs %s | %.2f→%.2f (+%.2f)",
                                    home, away, st.baseline, scorer_price, delta)
                    st.notified = True

            # Pulizia
            to_remove = []
            for k, v in match_state.items():
                age = now - v.first_seen_at
                loops_ago = _loop - v.last_seen_loop
                if loops_ago > 2 or age > 7200:
                    to_remove.append(k)
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
        raise SystemExit("❌ Variabili mancanti (TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY)")
    logger.info("="*60)
    logger.info("🚀 BOT QUOTE JUMP – NEW API (soccer-football-info)")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Min rise: +%.2f", MIN_RISE)
    logger.info("   • Range: %.2f-%.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Wait goal: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("   • Samples: %d (ogni %ds)", BASELINE_SAMPLES, BASELINE_SAMPLE_INTERVAL)
    logger.info("="*60)

    send_telegram_message(
        f"🤖 <b>Bot NEW API</b> ⚡\n\n"
        f"✅ 0-0 → 1-0/0-1\n"
        f"✅ Quote {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"✅ Rise <b>+{MIN_RISE:.2f}</b>\n"
        f"⏱ Minuto: <i>da payload live</i>\n"
        f"⚡ Wait <b>{WAIT_AFTER_GOAL_SEC}s</b> | {BASELINE_SAMPLES} samples ogni {BASELINE_SAMPLE_INTERVAL}s\n\n"
        f"🔍 Monitoraggio attivo!"
    )

    main_loop()

if __name__ == "__main__":
    main()
