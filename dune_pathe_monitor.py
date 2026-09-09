#!/usr/bin/env python3
"""Surveille toute trace de séance de Dune 3 à Pathé Odysseum."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CINEMA_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum"
IMAX_EVENT_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289"
PARIS = ZoneInfo("Europe/Paris")
DEFAULT_START = date(2026, 12, 16)
WEEKDAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
MONTHS_FR = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")


@dataclass(frozen=True)
class Session:
    date_label: str
    text: str

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.date_label}|{self.text}".encode()).hexdigest()[:16]


def normalise(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def setup_logging(path: Path) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return logging.getLogger("dune_pathe")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_session_keys": []}


def save_state(path: Path, sessions: list[Session]) -> None:
    path.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(PARIS).isoformat(),
                "active_session_keys": sorted(s.key for s in sessions),
                "sessions": [asdict(s) for s in sessions],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def notify_telegram(message: str, log: logging.Logger) -> None:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Nouvelle disponibilité, mais Telegram n'est pas configuré.")
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                log.error("Telegram a répondu %s", response.status)
    except Exception as exc:
        log.error("Envoi Telegram impossible : %s", exc)


def listed_dune_sessions(page, date_label: str) -> list[Session]:
    """Extrait toute séance Dune affichée, quel que soit son format ou son état.

    Une séance « COMPLET » est volontairement retenue : l'objectif est d'avoir
    une preuve que la programmation du film existe, pas de trouver une place.
    """
    results: list[Session] = []
    seen: set[str] = set()
    cards = page.locator("article, [class*='movie'], [class*='film'], [class*='schedule']").all()
    for card in cards:
        try:
            card_text = normalise(card.inner_text(timeout=3_000)).lower()
        except Exception:
            continue
        if "dune" not in card_text or ("troisième partie" not in card_text and "troisieme partie" not in card_text):
            continue
        for button in card.locator("button").all():
            try:
                text = normalise(button.inner_text(timeout=2_000))
                is_time = re.search(r"\b\d{1,2}[:h]\d{2}\b", text) is not None
                is_booking = "réserver" in text.lower() or "reserver" in text.lower()
                if not is_time and not is_booking:
                    continue
            except Exception:
                continue
            session = Session(date_label, text)
            if session.key not in seen:
                seen.add(session.key)
                results.append(session)
    return results


def pathé_date_label(day: date) -> str:
    """Libellé court utilisé par le carrousel de dates de Pathé, p.ex. « mer. 16 déc. »."""
    return f"{WEEKDAYS_FR[day.weekday()]} {day.day} {MONTHS_FR[day.month - 1]}"


def select_day(page, day: date) -> None:
    """Sélectionne un jour visible ; sinon l'absence est un résultat inconnu.

    On ne confond volontairement pas « ce jour n'est pas proposé par le site »
    et « aucune séance ce jour-là ». Les deux auraient des conséquences très
    différentes pour l'alerte.
    """
    label = pathé_date_label(day)
    choices = page.get_by_text(label, exact=True)
    if choices.count() == 0:
        raise RuntimeError(f"Le {label} n'est pas encore sélectionnable sur la page Pathé.")
    choices.first.click(timeout=10_000)
    page.wait_for_timeout(900)


def check_imax_event_page(browser) -> list[Session]:
    """Vérifie aussi la page spécifique de l'avant-première IMAX 70 mm.

    Cette page peut ouvrir la vente avant que la programmation générale du
    cinéma soit complète ; elle est donc une seconde source, contrôlée à chaque
    passage dès maintenant.
    """
    page = browser.new_page(locale="fr-FR", viewport={"width": 1440, "height": 1200})
    try:
        page.goto(IMAX_EVENT_URL, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2_500)
        body = normalise(page.locator("body").inner_text(timeout=15_000)).lower()
        if "dune" not in body or "troisième partie" not in body:
            raise RuntimeError("La page IMAX Dune attendue ne s'est pas chargée.")
        return listed_dune_sessions(page, "page IMAX 70 mm dédiée")
    finally:
        page.close()


def check(start: date, days: int, data_dir: Path) -> int:
    state_file = data_dir / "dune_pathe_state.json"
    log = setup_logging(data_dir / "dune_pathe_monitor.log")
    log.info("Période surveillée : %s au %s", start, start + timedelta(days=days - 1))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                sessions = []
                sources_ok = 0
                # La page IMAX est tentée séparément : elle reste utile même si
                # Pathé ne laisse pas encore choisir les dates 16–22 au cinéma.
                try:
                    sessions.extend(check_imax_event_page(browser))
                    sources_ok += 1
                except Exception as exc:
                    log.warning("Page IMAX non vérifiable : %s", exc)
                try:
                    page = browser.new_page(locale="fr-FR", viewport={"width": 1440, "height": 1200})
                    try:
                        page.goto(CINEMA_URL, wait_until="domcontentloaded", timeout=45_000)
                        page.wait_for_timeout(2_500)
                        body = normalise(page.locator("body").inner_text(timeout=15_000))
                        if "Pathé Odysseum" not in body:
                            raise RuntimeError("La page Pathé attendue ne s'est pas chargée.")
                        for offset in range(days):
                            day = start + timedelta(days=offset)
                            select_day(page, day)
                            sessions.extend(listed_dune_sessions(page, pathé_date_label(day)))
                        sources_ok += 1
                    finally:
                        page.close()
                except Exception as exc:
                    log.warning("Page cinéma non vérifiable : %s", exc)
                if sources_ok == 0:
                    raise RuntimeError("Aucune des deux pages Pathé n'a pu être vérifiée.")
            finally:
                browser.close()
    except (PlaywrightTimeoutError, RuntimeError) as exc:
        log.error("Résultat inconnu — état non modifié : %s", exc)
        return 2
    except Exception as exc:
        log.error("Vérification impossible — état non modifié : %s", exc)
        return 2

    old_keys = set(load_state(state_file).get("active_session_keys", []))
    fresh = [s for s in sessions if s.key not in old_keys]
    if fresh:
        message = "Dune 3 : nouvelle(s) séance(s) repérée(s) à Odysseum :\n"
        message += "\n".join(f"• {s.text}" for s in fresh) + f"\n{CINEMA_URL}"
        log.warning(message)
        notify_telegram(message, log)
    elif sessions:
        log.info("Séance(s) Dune déjà connue(s) : pas de doublon.")
    else:
        log.info("Aucune séance Dune affichée à cette vérification.")
    # À la différence du script Gemini, chaque passage recharge la page. Une
    # séance disparue puis remise en vente est à nouveau signalée.
    save_state(state_file, sessions)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Moniteur Dune 3 / Odysseum")
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days doit être positif")
    return check(args.start_date, args.days, args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())

