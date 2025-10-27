#!/usr/bin/env python3

-- coding: utf-8 --

""" Bot Quote Jump – First-Half Only

Requisiti richiesti:

Considera SOLO il primo gol del match: transizione 0-0 -> 1-0 oppure 0-1.

Ignora il minuto preciso: basta sapere se l'evento è nel PRIMO TEMPO.

Accetta la baseline soltanto se la quota del team che ha segnato è nel range 1.30–1.90 subito dopo il gol.

Notifica solo se, DOPO la baseline, la quota AUMENTA (anche di poco – soglia configurabile MIN_RISE).

Se l'evento non è nel primo tempo, scarta.

Correzioni/fix: robustezza chiavi odds, fairness tra match, cleanup veloce post notifica/rifiuto, gestione suspended, throttling, logging migliorato, uso orologio monotono per differenze.


NOTE IMPORTANTI:

Le funzioni di integrazione API get_odds_and_phase, can_call_odds_api, mark_odds_api_call, send_telegram_message, e il feed partite iter_live_matches() sono DA IMPLEMENTARE in base al provider (RapidAPI o altro). Il core della logica è completo e pronto. """


from future import annotations import os import time import random import logging from dataclasses import dataclass, field from typing import Dict, Optional, Tuple, Iterable

=========================

Config

=========================

MIN_RISE: float = 0.05          # incremento minimo assoluto per notificare (es. +0.05) BASELINE_MIN: float = 1.30 BASELINE_MAX: float = 1.90 WAIT_AFTER_GOAL_SEC: int = 8     # tempo minimo post-gol prima di iniziare a cercare la salita CHECK_INTERVAL: float = 1.0      # sleep tra loop MAX_ODDS_CALLS_PER_LOOP: int = 30 SUSPENDED_MAX_TRIES: int = 12    # oltre questo numero di letture "suspended" rifiuta CLEANUP_GRACE_SEC: int = 90      # quanto tenere lo stato dopo notified/rejected STATE_HARD_TTL_SEC: int = 7200   # 2 ore hard TTL

=========================

Env

=========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") CHAT_ID = os.getenv("CHAT_ID") RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

=========================

Logging

=========================

logging.basicConfig( level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", ) logger = logging.getLogger("quote_jump_firsthalf")

=========================

State

=========================

@dataclass class MatchState: eid: str home: str away: str league: str

first_seen_at: float = field(default_factory=time.time)
first_seen_score: Optional[Tuple[int, int]] = None

goal_time_wall: Optional[float] = None  # time.time() al gol
scoring_team: Optional[str] = None      # "home" / "away"
half_at_goal: Optional[int] = None      # 1 o 2

baseline: Optional[float] = None
baseline_set_at: Optional[float] = None

notified: bool = False
rejected_reason: Optional[str] = None
to_remove_at: Optional[float] = None

tries: int = 0
suspended_tries: int = 0
last_odds_check_ts: float = 0.0

=========================

Provider adapters (DA IMPLEMENTARE)

=========================

def iter_live_matches() -> Iterable[Tuple[str,str,str,str,Tuple[int,int]]]: """Yield di partite live presenti nel feed. Deve restituire tuple: (eid, home, away, league, cur_score) Esempio cur_score: (1,0) """ raise NotImplementedError

def get_odds_and_phase(eid: str, home: str, away: str) -> Tuple[Optional[Dict[str, float]], Optional[int]]: """Ritorna (odds_dict, half) - odds_dict: {"home": float|None, "away": float|None, "suspended": bool} - half: 1, 2 oppure None se non disponibile """ raise NotImplementedError

def can_call_odds_api() -> bool: return True

def mark_odds_api_call() -> None: pass

def send_telegram_message(html_text: str) -> None: """Invia HTML su Telegram. Da implementare con requests se necessario. """ logger.info("TELEGRAM >> %s", html_text.replace("\n", " | "))

=========================

Core logic

=========================

def main_loop() -> None: match_state: Dict[str, MatchState] = {}

while True:
    now = time.time()
    odds_calls = 0

    try:
        # Fairness: itera le partite in ordine random
        matches = list(iter_live_matches())
        random.shuffle(matches)

        for eid, home, away, league, cur_score in matches:
            st = match_state.get(eid)
            if st is None:
                st = MatchState(eid=eid, home=home, away=away, league=league, first_seen_score=cur_score)
                match_state[eid] = st

            # Se già notificato o rifiutato, aspetta il cleanup
            if st.notified or st.rejected_reason:
                if st.to_remove_at and now >= st.to_remove_at:
                    del match_state[eid]
                continue

            # STEP 1: verifica transizione primo gol 0-0 -> 1-0 / 0-1
            if st.goal_time_wall is None:
                first_score = st.first_seen_score or (0, 0)
                if first_score != (0, 0):
                    st.rejected_reason = "non_0-0_iniziale"
                    st.to_remove_at = now + CLEANUP_GRACE_SEC
                    continue

                if cur_score == (1, 0):
                    st.goal_time_wall = now
                    st.scoring_team = "home"
                    logger.info("⚽ Primo goal rilevato: %s vs %s (1-0) | %s | eid=%s", home, away, league, eid)
                elif cur_score == (0, 1):
                    st.goal_time_wall = now
                    st.scoring_team = "away"
                    logger.info("⚽ Primo goal rilevato: %s vs %s (0-1) | %s | eid=%s", home, away, league, eid)
                else:
                    # score invalido per primo goal
                    if cur_score != (0, 0):
                        st.rejected_reason = f"score_invalido_{cur_score[0]}-{cur_score[1]}"
                        st.to_remove_at = now + CLEANUP_GRACE_SEC
                    continue

            # Da qui: goal rilevato

            # STEP 2: accertati che siamo nel PRIMO TEMPO
            if not can_call_odds_api() or odds_calls >= MAX_ODDS_CALLS_PER_LOOP:
                continue
            odds, half = get_odds_and_phase(eid, home, away)
            mark_odds_api_call(); odds_calls += 1
            st.tries += 1

            if half is None:
                # se il provider non dice l'half, riprova al giro successivo
                continue
            if half != 1:
                st.rejected_reason = "non_primo_tempo"
                st.to_remove_at = now + CLEANUP_GRACE_SEC
                logger.info("⏭️ Goal NON nel primo tempo: %s vs %s | half=%s | eid=%s", home, away, half, eid)
                continue

            # STEP 3: attesa breve post-gol prima di iniziare la baseline
            if now - st.goal_time_wall < WAIT_AFTER_GOAL_SEC:
                continue

            # STEP 4: lettura quote valida e baseline in range
            if not odds:
                if st.tries > 30:
                    st.rejected_reason = "no_odds_30_tries"
                    st.to_remove_at = now + CLEANUP_GRACE_SEC
                    logger.warning("❌ No odds after 30 tries: %s vs %s | eid=%s", home, away, eid)
                continue

            if odds.get("suspended"):
                st.suspended_tries += 1
                if st.suspended_tries > SUSPENDED_MAX_TRIES:
                    st.rejected_reason = "suspended_too_long"
                    st.to_remove_at = now + CLEANUP_GRACE_SEC
                continue

            price = odds.get("home") if st.scoring_team == "home" else odds.get("away")
            if not isinstance(price, (int, float)):
                continue

            # se non abbiamo baseline, impostala se nel range
            if st.baseline is None:
                if not (BASELINE_MIN <= price <= BASELINE_MAX):
                    st.rejected_reason = f"quota_{price:.2f}_fuori_range"
                    st.to_remove_at = now + CLEANUP_GRACE_SEC
                    logger.info("❌ Baseline fuori range [%.2f-%.2f]: %.2f | %s vs %s | eid=%s",
                                BASELINE_MIN, BASELINE_MAX, price, home, away, eid)
                    continue
                st.baseline = float(price)
                st.baseline_set_at = now
                logger.info("✅ Baseline %.2f impostata | %s vs %s | scorer=%s | eid=%s",
                            st.baseline, home, away, st.scoring_team, eid)
                continue  # dal prossimo giro iniziamo a cercare la salita

            # STEP 5: monitoraggio salita nel primo tempo
            if half != 1:
                st.rejected_reason = "half_changed_to_2"
                st.to_remove_at = now + CLEANUP_GRACE_SEC
                continue

            delta = price - st.baseline
            if delta >= MIN_RISE:
                team_name = st.home if st.scoring_team == "home" else st.away
                team_label = "1" if st.scoring_team == "home" else "2"

                send_telegram_message(
                    (
                        f"💰💎 <b>OPPORTUNITÀ QUOTA – PRIMO TEMPO</b>\n\n"
                        f"🏆 {st.league}\n"
                        f"⚽ <b>{st.home}</b> vs <b>{st.away}</b>\n"
                        f"📊 Score: <b>{cur_score[0]}-{cur_score[1]}</b>\n"
                        f"⏱️ Tempo: <b>1°</b>\n\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{price:.2f}</b>\n"
                        f"Salita: <b>+{delta:.2f}</b>\n\n"
                        f"💎💰 NOTIFICA ATTIVA (solo 1° tempo) 💰💎"
                    )
                )

                logger.info("✅ ALERT: %s vs %s | %.2f → %.2f (+%.2f) | scorer=%s | eid=%s",
                            home, away, st.baseline, price, delta, st.scoring_team, eid)

                st.notified = True
                st.to_remove_at = now + CLEANUP_GRACE_SEC
                continue

            # Protezione: se lo score non è più quello del primo gol e non abbiamo ancora notificato, rifiuta
            expected = (1, 0) if st.scoring_team == "home" else (0, 1)
            if cur_score != expected and st.baseline is not None:
                st.rejected_reason = f"score_cambiato_a_{cur_score[0]}-{cur_score[1]}"
                st.to_remove_at = now + CLEANUP_GRACE_SEC
                continue

        # Cleanup periodico (hard TTL o grace scaduta)
        to_remove = []
        for k, v in match_state.items():
            if v.to_remove_at and now >= v.to_remove_at:
                to_remove.append(k)
            elif (now - v.first_seen_at) > STATE_HARD_TTL_SEC:
                to_remove.append(k)
        for k in to_remove:
            match_state.pop(k, None)

        time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logger.info("🛑 Stop richiesto dall'utente")
        break
    except Exception as e:
        logger.exception("Errore nel loop: %s", e)
        time.sleep(3)

=========================

Start

=========================

def main(): if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY]): raise SystemExit("❌ Missing env vars: TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY")

logger.info("="*60)
logger.info("🚀 BOT QUOTE JUMP – FIRST HALF ONLY")
logger.info("="*60)
logger.info("⚙️  Config:")
logger.info("   • Range baseline: %.2f - %.2f", BASELINE_MIN, BASELINE_MAX)
logger.info("   • Min rise: +%.2f", MIN_RISE)
logger.info("   • Wait after goal: %ds", WAIT_AFTER_GOAL_SEC)
logger.info("   • Check interval: %.1fs", CHECK_INTERVAL)
logger.info("   • Max odds calls per loop: %d", MAX_ODDS_CALLS_PER_LOOP)
logger.info("="*60)

send_telegram_message(
    (
        f"🤖 <b>Bot ATTIVO – Solo PRIMO TEMPO</b>\n\n"
        f"✅ Solo <b>0-0 → 1-0/0-1</b> (primo gol)\n"
        f"✅ Considero solo eventi nel <b>1° tempo</b>\n"
        f"✅ Quota post-gol in range <b>{BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}</b>\n"
        f"✅ Notifico se la quota <b>sale</b> di almeno <b>+{MIN_RISE:.2f}</b>\n"
    )
)

main_loop()

if name == "main": main()
