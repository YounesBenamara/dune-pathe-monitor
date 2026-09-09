#!/usr/bin/env python3
"""Surveille toute trace de séance ou prévente de Dune 3 à Pathé Odysseum (Montpellier)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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
DEFAULT_DAYS = 7
WEEKDAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
MONTHS_FR = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")

STEALTH_INIT_SCRIPT = """
// Masquage de navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

// Simulation de l'objet chrome
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// Plugins factices
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

// Langues francophones par défaut
Object.defineProperty(navigator, 'languages', {
    get: () => ['fr-FR', 'fr', 'en-US', 'en']
});
"""


def is_dune_3(text: str) -> bool:
    """Détecte les mentions spécifiques à Dune 3 / Troisième partie / Part Three."""
    t = text.lower()
    if "dune" not in t:
        return False
    keywords = (
        "troisième",
        "troisieme",
        "partie 3",
        "part 3",
        "part three",
        "partie iii",
        "part iii",
        "dune 3",
        "messiah",
        "messie",
    )
    return any(kw in t for kw in keywords)


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


def notify_telegram(message: str, log: logging.Logger) -> bool:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        log.warning("Telegram non configuré (TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant).")
        return False
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                log.error("Telegram a répondu avec le statut %s", response.status)
                return False
            return True
    except Exception as exc:
        log.error("Envoi Telegram impossible : %s", exc)
        return False


def notify_ntfy(message: str, log: logging.Logger) -> bool:
    """Envoie une notification push ntfy vers un topic public ou protégé."""
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        log.warning("ntfy non configuré (NTFY_TOPIC manquant).")
        return False

    server = (os.getenv("NTFY_SERVER") or "").strip().rstrip("/") or "https://ntfy.sh"
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    headers = {
        "Title": "Dune 3 - Pathe Odysseum",
        "Priority": "urgent",
        "Tags": "movie_camera,ticket",
    }
    token = (os.getenv("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{server}/{topic}"
    try:
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            if 200 <= response.status < 300:
                return True
            log.error("ntfy a répondu avec le statut %s", response.status)
            return False
    except Exception as exc:
        log.error("Envoi ntfy impossible : %s", exc)
        return False


def notify_all(message: str, log: logging.Logger) -> None:
    """Alerte Telegram et ntfy en parallèle pour un envoi instantané."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_tg = executor.submit(notify_telegram, message, log)
        future_ntfy = executor.submit(notify_ntfy, message, log)
        res_tg = future_tg.result()
        res_ntfy = future_ntfy.result()

    notified = []
    if res_tg:
        notified.append("Telegram")
    if res_ntfy:
        notified.append("ntfy")
    log.info("Canaux notifiés en parallèle : %s", ", ".join(notified) or "aucun")


def pathé_date_label(day: date) -> str:
    """Libellé court utilisé par le carrousel de dates de Pathé, p.ex. « mer. 16 déc. »."""
    return f"{WEEKDAYS_FR[day.weekday()]} {day.day} {MONTHS_FR[day.month - 1]}"


def create_stealth_context(browser):
    """Crée un contexte de navigation avec évasion anti-bot."""
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="fr-FR",
        timezone_id="Europe/Paris",
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        },
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)

    # Blocage des ressources lourdes et superflues (images, médias, polices, traceurs publicitaires)
    # Accélère le chargement de la page de 3x à 5x
    blocked_exts = re.compile(r"\.(png|jpe?g|webp|svg|gif|woff2?|ttf|eot|mp4|webm|avi)$", re.IGNORECASE)
    blocked_trackers = re.compile(
        r"(google-analytics|googletagmanager|doubleclick|criteo|facebook|tiktok|eulerian|hotjar|optimizely)",
        re.IGNORECASE,
    )

    def route_filter(route):
        url = route.request.url
        if blocked_exts.search(url) or blocked_trackers.search(url):
            route.abort()
        else:
            route.continue_()

    context.route("**/*", route_filter)
    return context


def dismiss_overlays(page) -> None:
    """Ferme les bannières de cookies ou fenêtres contextuelles si présentes."""
    for selector in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Tout accepter')",
        "button:has-text('Continuer sans accepter')",
        "button:has-text('Accepter')",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=500)
                page.wait_for_timeout(150)
                break
        except Exception:
            continue


def is_valid_showtime(text: str) -> bool:
    """Vérifie si le texte contient une heure de séance (ex: 14h30, 20:15) en évitant les durées de film (ex: 2h25)."""
    match = re.search(r"\b(0?[0-9]|1[0-9]|2[0-3])[:h]([0-5][0-9])\b", text)
    if not match:
        return False
    hour = int(match.group(1))
    # Horaires réalistes de séances de cinéma (09h00 à 23h59 ou séances tardives 00h-01h)
    return 9 <= hour <= 23 or hour <= 1


def listed_dune_sessions(page, context_label: str) -> list[Session]:
    """Extrait les séances pour Dune 3 : réservables, avec horaire, ou indiquées COMPLET."""
    results: list[Session] = []
    seen: set[str] = set()

    cards = page.locator(
        "article, [class*='movie'], [class*='film'], [class*='schedule'], [class*='card']"
    ).all()

    for card in cards:
        try:
            card_text = normalise(card.inner_text(timeout=1_500))
        except Exception:
            continue

        if not is_dune_3(card_text):
            continue

        # Recherche de créneaux de séances : boutons, liens de résa, badges complet
        elements = card.locator(
            "button, a[href*='reservation'], a[href*='booking'], a[href*='billet'], "
            "[class*='session'], [class*='showtime'], [class*='slot'], [class*='complet']"
        ).all()

        for el in elements:
            try:
                text = normalise(el.inner_text(timeout=800))
                if not text:
                    continue
                t_lower = text.lower()
                is_time = is_valid_showtime(text)
                is_booking = any(w in t_lower for w in ["réserver", "reserver", "billet", "acheter"])
                is_complet = "complet" in t_lower

                # On retient la séance si elle a un vrai horaire, un bouton de réservation ou si elle est COMPLET
                if is_time or is_booking or is_complet:
                    if is_complet:
                        desc = f"Séance (COMPLET) : {text}"
                    elif is_booking:
                        desc = f"Séance réservable : {text}"
                    else:
                        desc = f"Séance programmée : {text}"

                    session = Session(context_label, f"{desc} ({card_text[:60]}...)")
                    if session.key not in seen:
                        seen.add(session.key)
                        results.append(session)
            except Exception:
                continue

    return results


def select_day_if_available(page, day: date, log: logging.Logger) -> bool:
    """Tente de sélectionner un jour dans le carrousel si disponible (non bloquant)."""
    label = pathé_date_label(day)
    try:
        choices = page.get_by_text(label, exact=True)
        if choices.count() == 0:
            log.info("Date %s non présente dans le carrousel (réservation lointaine).", label)
            return False
        choices.first.click(timeout=2_000)
        page.wait_for_timeout(250)
        return True
    except Exception as exc:
        log.debug("Impossible de cliquer sur %s : %s", label, exc)
        return False


def check_imax_event_page(context, log: logging.Logger) -> list[Session]:
    """Vérifie la page dédiée à l'événement / projection IMAX si elle existe."""
    page = context.new_page()
    try:
        try:
            response = page.goto(IMAX_EVENT_URL, wait_until="domcontentloaded", timeout=20_000)
            if response and response.status >= 400:
                log.info("Page événement IMAX non accessible (HTTP %s).", response.status)
                return []
        except Exception as exc:
            log.info("Page événement IMAX non joignable pour le moment : %s", exc)
            return []

        dismiss_overlays(page)
        try:
            page.wait_for_selector("article, h1, [class*='event'], [class*='movie'], body", timeout=2_000)
        except Exception:
            pass

        body = normalise(page.locator("body").inner_text(timeout=3_000))
        if not is_dune_3(body):
            log.info("Page IMAX chargée mais Dune 3 n'y figure pas encore.")
            return []
        return listed_dune_sessions(page, "Avant-première IMAX 70mm")
    finally:
        page.close()


def check_cinema_page(context, start: date, days: int, log: logging.Logger) -> list[Session]:
    """Vérifie la programmation sur la page du cinéma Pathé Odysseum."""
    page = context.new_page()
    try:
        response = None
        for attempt in range(2):
            try:
                response = page.goto(CINEMA_URL, wait_until="domcontentloaded", timeout=30_000)
                if response and response.status == 403:
                    page.wait_for_timeout(1_000)
                    probe = normalise(page.locator("body").inner_text(timeout=2_000))
                    if "Pathé" in probe or "Odysseum" in probe:
                        break
                    if attempt == 0:
                        page.wait_for_timeout(1_000)
                        page.reload(wait_until="domcontentloaded", timeout=20_000)
                break
            except Exception as exc:
                if attempt == 1:
                    raise exc

        dismiss_overlays(page)

        # Attente dynamique ultra-rapide du contenu réel
        try:
            page.wait_for_selector("article, [class*='movie'], [class*='film'], h1, footer", timeout=3_000)
        except Exception:
            pass

        body = normalise(page.locator("body").inner_text(timeout=3_000))
        title = page.title()
        if not any(k in body.lower() or k in title.lower() for k in ["pathé", "pathe", "odysseum"]):
            log.warning("Page cinéma non validée (title: %r, snippet: %r)", title, body[:120])

        sessions: list[Session] = []

        # 1. Vérification générale sur la page (films à l'affiche et annonces)
        sessions.extend(listed_dune_sessions(page, "Pathé Odysseum (Général)"))

        # 2. Vérification des dates cibles si affichées dans le carrousel
        for offset in range(days):
            day = start + timedelta(days=offset)
            if select_day_if_available(page, day, log):
                sessions.extend(listed_dune_sessions(page, pathé_date_label(day)))

        return sessions
    finally:
        page.close()


def check(start: date, days: int, data_dir: Path, force_notify: bool = False) -> int:
    state_file = data_dir / "dune_pathe_state.json"
    log = setup_logging(data_dir / "dune_pathe_monitor.log")
    end_date = start + timedelta(days=days - 1)
    log.info(
        "Vérification quotidienne Dune 3 — Pathé Odysseum (période cible : %s au %s, + avant-première IMAX 70mm)",
        start,
        end_date,
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                ],
            )
            try:
                context = create_stealth_context(browser)
                sessions: list[Session] = []
                sources_ok = 0

                # 1. Source événement IMAX
                try:
                    imax_sessions = check_imax_event_page(context, log)
                    sessions.extend(imax_sessions)
                    sources_ok += 1
                except Exception as exc:
                    log.warning("Erreur vérification page IMAX : %s", exc)

                # 2. Source cinéma Pathé Odysseum
                try:
                    cinema_sessions = check_cinema_page(context, start, days, log)
                    sessions.extend(cinema_sessions)
                    sources_ok += 1
                except Exception as exc:
                    log.error("Erreur vérification page cinéma : %s", exc)

                if sources_ok == 0:
                    raise RuntimeError("Aucune des sources Pathé n'a pu être vérifiée.")
            finally:
                browser.close()
    except (PlaywrightTimeoutError, RuntimeError) as exc:
        log.error("Résultat inconnu — état non modifié : %s", exc)
        return 2
    except Exception as exc:
        log.error("Vérification impossible — état non modifié : %s", exc)
        return 2

    # Dédoublonnage
    unique_sessions: list[Session] = []
    seen_keys: set[str] = set()
    for s in sessions:
        if s.key not in seen_keys:
            seen_keys.add(s.key)
            unique_sessions.append(s)

    old_keys = set(load_state(state_file).get("active_session_keys", []))
    fresh = [s for s in unique_sessions if s.key not in old_keys]

    if force_notify and not fresh:
        if unique_sessions:
            fresh = unique_sessions
        else:
            fresh = [Session("Test manuel", "Notification de test : votre moniteur Telegram et ntfy fonctionne parfaitement !")]

    if fresh:
        message = (
            "🚨 Dune 3 : séance(s) / prévente(s) repérée(s) au Pathé Odysseum !\n\n"
        )
        message += "\n".join(f"• [{s.date_label}] {s.text}" for s in fresh)
        message += f"\n\nLien de réservation :\n{CINEMA_URL}"
        log.warning(message)
        notify_all(message, log)
    elif unique_sessions:
        log.info("Séance(s) Dune déjà connue(s) (%d séance(s)) : pas de doublon.", len(unique_sessions))
    else:
        log.info("Aucune séance ni trace de Dune 3 affichée à cette vérification.")

    save_state(state_file, unique_sessions)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Moniteur Dune 3 / Odysseum")
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--force-notify", action="store_true", help="Force l'envoi d'une notification de test")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days doit être positif")
    return check(args.start_date, args.days, args.data_dir, args.force_notify)


if __name__ == "__main__":
    raise SystemExit(main())
