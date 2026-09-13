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
DECEMBER_15_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-15"
DECEMBER_16_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-16"
IMAX_EVENT_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289"
IMAX_15_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289/filters/date-2026-12-15"
IMAX_18_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289/filters/date-2026-12-18"

PARIS = ZoneInfo("Europe/Paris")
DEFAULT_START = date(2026, 12, 15)  # Surveillance dès l'avant-première du 15 décembre
DEFAULT_DAYS = 8  # Du 15 au 22 décembre inclus
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


def cinema_filter_url(day: date) -> str:
    """Génère l'URL officielle Pathé avec filtre de date dans le chemin."""
    return f"{CINEMA_URL}/filters/date-{day.isoformat()}"


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
    url: str = ""

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


def safe_ascii_header(text: str) -> str:
    """Garantit qu'un en-tête HTTP est encodable en latin-1/ASCII strict."""
    clean = text.replace("—", "-").replace("–", "-")
    return clean.encode("latin-1", "replace").decode("latin-1")


def notify_ntfy(
    message: str,
    log: logging.Logger,
    title: str = "Dune 3 - Pathe Odysseum",
    tags: str = "movie_camera,ticket",
    priority: str = "urgent",
    click_url: str = DECEMBER_15_URL,
) -> bool:
    """Envoie une notification push ntfy avec lien cliquable et action directe."""
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        log.warning("ntfy non configuré (NTFY_TOPIC manquant).")
        return False

    server = (os.getenv("NTFY_SERVER") or "").strip().rstrip("/") or "https://ntfy.sh"
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    headers = {
        "Title": safe_ascii_header(title),
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"view, Ouvrir la séance, {click_url}"

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


def notify_all(
    message: str,
    log: logging.Logger,
    title: str = "Dune 3 - Pathe Odysseum",
    tags: str = "movie_camera,ticket",
    priority: str = "urgent",
    click_url: str = DECEMBER_15_URL,
) -> None:
    """Alerte Telegram et ntfy en parallèle pour un envoi instantané."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_tg = executor.submit(notify_telegram, message, log)
        future_ntfy = executor.submit(notify_ntfy, message, log, title, tags, priority, click_url)
        res_tg = future_tg.result()
        res_ntfy = future_ntfy.result()

    notified = []
    if res_tg:
        notified.append("Telegram")
    if res_ntfy:
        notified.append("ntfy")
    log.info("Canaux notifiés en parallèle : %s", ", ".join(notified) or "aucun")


def pathé_date_label(day: date) -> str:
    """Libellé court utilisé par le carrousel de dates de Pathé, p.ex. « mar. 15 déc. »."""
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

    # Blocage des ressources lourdes et traceurs pour un chargement 3x plus rapide
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
    return 9 <= hour <= 23 or hour <= 1


def listed_dune_sessions(page, context_label: str) -> list[Session]:
    """Extrait les séances pour Dune 3 : réservables, avec horaire, ou indiquées COMPLET."""
    results: list[Session] = []
    seen: set[str] = set()

    cards = page.locator(
        "article, [class*='movie'], [class*='film'], [class*='schedule'], [class*='card'], [data-testid*='movie']"
    ).all()

    for card in cards:
        try:
            card_text = normalise(card.inner_text(timeout=1_500))
        except Exception:
            continue

        if not is_dune_3(card_text):
            continue

        elements = card.locator(
            "button, a, time, [class*='session'], [class*='showtime'], [class*='slot'], [class*='complet']"
        ).all()

        for el in elements:
            try:
                text = normalise(el.inner_text(timeout=800))
                if not text or len(text) > 40:
                    continue
                t_lower = text.lower()
                is_time = is_valid_showtime(text)
                is_booking = any(w in t_lower for w in ["réserver", "reserver", "billet", "acheter"])
                is_complet = "complet" in t_lower

                if is_time or is_booking or is_complet:
                    if is_complet:
                        desc = f"Séance (COMPLET) : {text}"
                    elif is_booking:
                        desc = f"Séance réservable : {text}"
                    else:
                        desc = f"Séance programmée : {text}"

                    href = ""
                    try:
                        href = el.get_attribute("href") or ""
                        if not href:
                            parent_a = el.locator("xpath=ancestor-or-self::a").first
                            if parent_a.count() > 0:
                                href = parent_a.get_attribute("href") or ""
                        if href and not href.startswith("http"):
                            href = urllib.parse.urljoin("https://www.pathe.fr", href)
                    except Exception:
                        href = ""

                    session = Session(context_label, f"{desc} ({card_text[:60]}...)", url=href)
                    if session.key not in seen:
                        seen.add(session.key)
                        results.append(session)
            except Exception:
                continue

    return results


def send_immediate_alert(
    fresh_sessions: list[Session],
    direct_url: str,
    source_name: str,
    log: logging.Logger,
) -> None:
    """Envoie une alerte immédiate (Telegram + ntfy en parallèle) dès détection d'une séance."""
    message = f"🚨 DUNE 3 – SÉANCE DISPONIBLE AU PATHÉ ODYSSEUM !\n({source_name})\n\n"
    lines = []
    first_res_url = ""
    for s in fresh_sessions:
        item = f"• [{s.date_label}] {s.text}"
        if s.url:
            item += f"\n  👉 RÉSERVER ICI : {s.url}"
            if not first_res_url:
                first_res_url = s.url
        lines.append(item)
    message += "\n".join(lines)
    message += (
        f"\n\n📅 Lien direct séance :\n{direct_url}\n\n"
        f"📅 Avant-première 15 décembre :\n{DECEMBER_15_URL}\n"
        f"📅 Sortie nationale 16 décembre :\n{DECEMBER_16_URL}\n"
        f"🎟️ Événement IMAX 70mm :\n{IMAX_EVENT_URL}\n\n"
        "⚡ Ouvre le lien le plus haut le plus vite possible !"
    )

    click_target = first_res_url or direct_url or DECEMBER_15_URL
    log.warning(message)
    notify_all(
        message,
        log,
        title=f"🚨 DUNE 3 DISPONIBLE – {source_name}",
        tags="rotating_light,ticket,cinema",
        priority="urgent",
        click_url=click_target,
    )


def check_imax_event_page(context, log: logging.Logger) -> list[Session]:
    """Vérifie la page dédiée à l'événement / projection IMAX si elle existe."""
    page = context.new_page()
    try:
        for url in [IMAX_15_URL, IMAX_EVENT_URL]:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                if response and response.status >= 400:
                    continue
                dismiss_overlays(page)
                body = normalise(page.locator("body").inner_text(timeout=2_500))
                if is_dune_3(body):
                    sessions = listed_dune_sessions(page, "Avant-première IMAX 70mm")
                    if sessions:
                        return sessions
            except Exception as exc:
                log.debug("Erreur accès IMAX %s : %s", url, exc)
                continue
        log.info("Page IMAX vérifiée : Dune 3 n'y figure pas encore.")
        return []
    finally:
        page.close()


def check_single_date_page(page, day: date, log: logging.Logger) -> list[Session]:
    """Vérifie la programmation d'une date spécifique via son URL officielle avec filtre."""
    day_url = cinema_filter_url(day)
    try:
        response = page.goto(day_url, wait_until="domcontentloaded", timeout=25_000)
        if response and response.status in (403, 429, 503):
            page.wait_for_timeout(1_000)
            probe = normalise(page.locator("body").inner_text(timeout=2_000))
            if "Pathé" not in probe and "Odysseum" not in probe:
                log.warning("Page date %s bloquée par protection anti-bot (HTTP %s)", day, response.status)
                return []
    except Exception as exc:
        log.warning("Impossible de charger la page du %s : %s", day, exc)
        return []

    dismiss_overlays(page)

    try:
        page.wait_for_selector("article, [class*='movie'], [class*='film'], h1, footer", timeout=2_000)
    except Exception:
        pass

    body = normalise(page.locator("body").inner_text(timeout=2_000))
    title = page.title()
    if not any(k in body.lower() or k in title.lower() for k in ["pathé", "pathe", "odysseum"]):
        log.info("Page date %s : contenu non reconnu ou inaccessible (titre: %r)", day, title)
        return []

    if not is_dune_3(body):
        log.info("Page %s vérifiée : aucune trace de Dune 3.", day)
        return []

    log.info("🎯 DUNE 3 détecté dans la page du %s ! Analyse des séances...", day)
    return listed_dune_sessions(page, pathé_date_label(day))


def check(
    start: date = DEFAULT_START,
    days: int = DEFAULT_DAYS,
    data_dir: Path = Path(__file__).parent,
    force_notify: bool = False,
) -> int:
    state_file = data_dir / "dune_pathe_state.json"
    log = setup_logging(data_dir / "dune_pathe_monitor.log")
    end_date = start + timedelta(days=days - 1)
    log.info(
        "Vérification ultra-rapide Dune 3 — Pathé Odysseum (période cible : %s au %s, + avant-première IMAX 70mm)",
        start,
        end_date,
    )

    state = load_state(state_file)
    known_keys = set(state.get("active_session_keys", []))
    all_detected_sessions: list[Session] = []
    total_fresh_notified = 0
    force_notified_sent = False

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
                sources_ok = 0

                # 1. ÉTAPE PRIORITAIRE N°1 : Le 15 décembre (Avant-première la veille)
                cinema_page = context.new_page()
                try:
                    day_15_sessions = check_single_date_page(cinema_page, start, log)
                    all_detected_sessions.extend(day_15_sessions)
                    sources_ok += 1

                    fresh_15 = [s for s in day_15_sessions if s.key not in known_keys]
                    if fresh_15:
                        send_immediate_alert(fresh_15, DECEMBER_15_URL, "15 décembre (Avant-première)", log)
                        for s in fresh_15:
                            known_keys.add(s.key)
                        total_fresh_notified += len(fresh_15)
                        state["active_session_keys"] = list(known_keys)
                        save_state(state_file, all_detected_sessions)
                    elif force_notify and not force_notified_sent:
                        # Test manuel envoyé immédiatement dès la vérification du 15 décembre
                        test_msg = (
                            "🧪 [TEST MANUEL] Moniteur Dune 3 — Pathé Odysseum\n\n"
                            "✅ Vos notifications Telegram et ntfy fonctionnent parfaitement !\n\n"
                            "ℹ️ Le 15 décembre (avant-première) et le 16 décembre (sortie nationale) ont été vérifiés en priorité.\n"
                            "Aucune séance n'est ouverte pour le moment (zéro faux positif).\n"
                            "La surveillance continue pour l'IMAX 70mm et les autres dates.\n\n"
                            "🔗 Liens directs avec filtres :\n"
                            f"📅 Avant-première 15 décembre :\n{DECEMBER_15_URL}\n\n"
                            f"📅 Sortie nationale 16 décembre :\n{DECEMBER_16_URL}\n\n"
                            f"🎟️ Événement IMAX 70mm :\n{IMAX_EVENT_URL}\n"
                            f"{IMAX_18_URL}"
                        )
                        log.info("Envoi immédiat du test manuel dès la vérification du 15 décembre.")
                        notify_all(
                            test_msg,
                            log,
                            title="[TEST] Moniteur Dune 3 - Pathe Odysseum",
                            tags="test_tube,white_check_mark",
                            priority="default",
                            click_url=DECEMBER_15_URL,
                        )
                        force_notified_sent = True

                    # 2. ÉTAPE PRIORITAIRE N°2 : Le 16 décembre (Jour de sortie nationale)
                    day_16 = date(2026, 12, 16)
                    day_16_sessions = check_single_date_page(cinema_page, day_16, log)
                    all_detected_sessions.extend(day_16_sessions)
                    sources_ok += 1

                    fresh_16 = [s for s in day_16_sessions if s.key not in known_keys]
                    if fresh_16:
                        send_immediate_alert(fresh_16, DECEMBER_16_URL, "16 décembre (Sortie nationale)", log)
                        for s in fresh_16:
                            known_keys.add(s.key)
                        total_fresh_notified += len(fresh_16)
                        state["active_session_keys"] = list(known_keys)
                        save_state(state_file, all_detected_sessions)

                    # 3. ÉTAPE PRIORITAIRE N°3 : Page Événement Avant-première IMAX 70mm
                    try:
                        imax_sessions = check_imax_event_page(context, log)
                        all_detected_sessions.extend(imax_sessions)
                        sources_ok += 1

                        fresh_imax = [s for s in imax_sessions if s.key not in known_keys]
                        if fresh_imax:
                            send_immediate_alert(fresh_imax, IMAX_EVENT_URL, "Avant-première IMAX 70mm", log)
                            for s in fresh_imax:
                                known_keys.add(s.key)
                            total_fresh_notified += len(fresh_imax)
                            state["active_session_keys"] = list(known_keys)
                            save_state(state_file, all_detected_sessions)
                    except Exception as exc:
                        log.warning("Erreur vérification page IMAX : %s", exc)

                    # 4. ÉTAPE DATES SUIVANTES (17 au 22 décembre)
                    for offset in range(2, days):
                        day = start + timedelta(days=offset)
                        day_url = cinema_filter_url(day)
                        try:
                            day_sessions = check_single_date_page(cinema_page, day, log)
                            all_detected_sessions.extend(day_sessions)
                            sources_ok += 1

                            fresh_day = [s for s in day_sessions if s.key not in known_keys]
                            if fresh_day:
                                label = pathé_date_label(day)
                                send_immediate_alert(fresh_day, day_url, f"Séances du {label}", log)
                                for s in fresh_day:
                                    known_keys.add(s.key)
                                total_fresh_notified += len(fresh_day)
                                state["active_session_keys"] = list(known_keys)
                                save_state(state_file, all_detected_sessions)
                        except Exception as day_exc:
                            log.error("Erreur vérification date %s : %s", day, day_exc)
                finally:
                    cinema_page.close()

                if sources_ok == 0:
                    raise RuntimeError("Aucune des sources Pathé n'a pu être vérifiée.")
            finally:
                browser.close()
    except (PlaywrightTimeoutError, RuntimeError, Exception) as exc:
        log.error("Vérification impossible — état non modifié : %s", exc)
        try:
            state = load_state(state_file)
            if not state.get("in_error"):
                err_msg = (
                    "⚠️ Alerte Moniteur Dune 3 : Problème d'accès au site Pathé Odysseum\n\n"
                    f"Détail : {exc}\n\n"
                    "Le site Pathé ou sa protection anti-bot bloque la connexion ou est temporairement inaccessible. "
                    "Une nouvelle tentative aura lieu au prochain créneau programmé."
                )
                notify_all(
                    err_msg,
                    log,
                    title="Alerte : Acces Pathe bloque",
                    tags="warning,shield",
                    priority="high",
                    click_url=DECEMBER_15_URL,
                )
                state["in_error"] = True
                state["last_error"] = str(exc)
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as notify_err:
            log.error("Erreur lors de l'alerte d'échec : %s", notify_err)
        return 2

    # Rétablissement après incident si nécessaire
    state = load_state(state_file)
    if state.get("in_error"):
        recover_msg = (
            "✅ Moniteur Dune 3 : Accès au site Pathé Odysseum rétabli !\n\n"
            "La vérification a pu s'effectuer normalement avec succès."
        )
        notify_all(
            recover_msg,
            log,
            title="Acces Pathe retabli",
            tags="white_check_mark",
            priority="default",
            click_url=DECEMBER_15_URL,
        )
        state["in_error"] = False
        state["last_error"] = None

    # Dédoublonnage global
    unique_sessions: list[Session] = []
    seen_keys: set[str] = set()
    for s in all_detected_sessions:
        if s.key not in seen_keys:
            seen_keys.add(s.key)
            unique_sessions.append(s)

    if force_notify:
        if unique_sessions and total_fresh_notified == 0:
            send_immediate_alert(unique_sessions, DECEMBER_15_URL, "Séances disponibles", log)
        elif not force_notified_sent and total_fresh_notified == 0:
            test_message = (
                "🧪 [TEST MANUEL] Moniteur Dune 3 — Pathé Odysseum\n\n"
                "✅ Vos notifications Telegram et ntfy fonctionnent parfaitement !\n\n"
                "ℹ️ Aucune séance n'est ouverte pour le moment (zéro faux positif).\n"
                "La surveillance automatique est active selon le planning programmé.\n\n"
                "🔗 Liens directs avec filtres :\n"
                f"📅 Avant-première 15 décembre :\n{DECEMBER_15_URL}\n\n"
                f"📅 Sortie nationale 16 décembre :\n{DECEMBER_16_URL}\n\n"
                f"🎟️ Événement IMAX 70mm :\n{IMAX_EVENT_URL}\n"
                f"{IMAX_18_URL}"
            )
            log.info("Envoi de la notification de test manuel (sans fausse alerte).")
            notify_all(
                test_message,
                log,
                title="[TEST] Moniteur Dune 3 - Pathe Odysseum",
                tags="test_tube,white_check_mark",
                priority="default",
                click_url=DECEMBER_15_URL,
            )
    else:
        if unique_sessions and total_fresh_notified == 0:
            log.info("Séance(s) Dune déjà connue(s) (%d séance(s)) : pas de doublon.", len(unique_sessions))
        elif not unique_sessions:
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
