#!/usr/bin/env python3
"""Moniteur LOCAL permanent pour Dune 3 à Pathé Odysseum (Montpellier).

Tourne 24h/24 en arrière-plan sur ton PC :
1. Tente la vérification directe sur pathe.fr avec Google Chrome (mode furtif anti-détection).
2. Effectue en parallèle la vérification AlloCiné comme filet de sécurité instantané.
3. Dès détection d'une séance (notamment IMAX 70mm le 16 ou 15 décembre), envoie l'alerte
   immédiate sur Telegram et ntfy avec les liens directs de réservation Pathé.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Assurer l'encodage UTF-8 pour la console Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

PARIS = ZoneInfo("Europe/Paris")

# URLs Pathé Odysseum
PATHE_DEC_16_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-16"
PATHE_DEC_15_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-15"
PATHE_IMAX_EVENT_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289"

# URLs AlloCiné (Secours / Comparaison)
THEATER_CODE = "P0702"
ALLOCINE_16_API = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-2026-12-16"
ALLOCINE_15_API = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-2026-12-15"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR', 'fr', 'en-US', 'en'] });
"""


@dataclass(frozen=True)
class DetectedSession:
    source: str
    date_str: str
    label: str
    format_str: str
    is_imax: bool
    url: str

    @property
    def key(self) -> str:
        raw = f"{self.date_str}|{self.label}|{self.format_str}|{self.url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_env_file(env_path: Path) -> None:
    """Charge un fichier .env local s'il existe."""
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if k and not os.getenv(k):
            os.environ[k] = v


def setup_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_h = logging.FileHandler(path, encoding="utf-8")
    file_h.setFormatter(formatter)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(formatter)

    logger = logging.getLogger("local_pathe_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(file_h)
    logger.addHandler(stream_h)
    return logger


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_keys": []}


def save_state(path: Path, sessions: list[DetectedSession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(PARIS).isoformat(),
                "active_keys": sorted(s.key for s in sessions),
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
        log.warning("Telegram non configuré (TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env).")
        return False
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except Exception as exc:
        log.error("Envoi Telegram échoué : %s", exc)
        return False


def notify_ntfy(
    message: str,
    log: logging.Logger,
    title: str = "Dune 3 - Pathé Odysseum",
    tags: str = "movie_camera,ticket",
    priority: str = "urgent",
    click_url: str = PATHE_DEC_16_URL,
) -> bool:
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        log.warning("ntfy non configuré (NTFY_TOPIC manquant dans .env).")
        return False

    server = (os.getenv("NTFY_SERVER") or "").strip().rstrip("/") or "https://ntfy.sh"
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    priority_map = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
    pri_int = priority_map.get(priority.lower(), 5)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    payload_data = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": pri_int,
        "tags": tag_list,
    }

    if click_url:
        payload_data["click"] = click_url
        payload_data["actions"] = [
            {"action": "view", "label": "🎟️ Réserver sur Pathé", "url": click_url}
        ]

    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "dune-local-monitor/1.0"}
    token = (os.getenv("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        request = urllib.request.Request(
            server,
            data=json.dumps(payload_data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        log.error("Envoi ntfy échoué : %s", exc)
        return False


def notify_all(
    message: str,
    log: logging.Logger,
    title: str = "🚨 DUNE 3 — PATHÉ ODYSSEUM",
    tags: str = "rotating_light,ticket,popcorn",
    priority: str = "urgent",
    click_url: str = PATHE_DEC_16_URL,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_tg = executor.submit(notify_telegram, message, log)
        f_ntfy = executor.submit(notify_ntfy, message, log, title, tags, priority, click_url)
        res_tg, res_ntfy = f_tg.result(), f_ntfy.result()

    notified = []
    if res_tg:
        notified.append("Telegram")
    if res_ntfy:
        notified.append("ntfy")
    log.info("Canaux alertés en direct : %s", ", ".join(notified) or "aucun")


def is_dune_3(text: str) -> bool:
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


def check_pathe_via_chrome(page, url: str, label: str, log: logging.Logger) -> list[DetectedSession]:
    """Tente la navigation directe vers Pathé via le Chrome local furtif."""
    sessions: list[DetectedSession] = []
    try:
        log.info("[Pathé] Chargement de %s (%s)...", label, url)
        res = page.goto(url, wait_until="domcontentloaded", timeout=18000)
        status = res.status if res else 0

        # Fermer cookies ou bannières
        for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accepter')"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click(timeout=500)
                    break
            except Exception:
                pass

        try:
            body = page.locator("body").inner_text(timeout=2500)
        except Exception:
            body = page.content()
        title = page.title()

        if "Allo Houston" in body or status == 403:
            log.warning("[Pathé] Akamai a renvoyé 'Allo Houston' (403) pour %s.", label)
            return []

        if not is_dune_3(body) and not is_dune_3(title):
            log.info("[Pathé] %s chargé (HTTP %s) : pas de séance Dune 3 ouverte.", label, status)
            return []

        log.warning("🎯 [Pathé] DUNE 3 DÉTECTÉ SUR %s !", label)

        # Vérifier si des créneaux de séances horaires sont présents (ex: 20h15, Réserver)
        has_sessions = any(w in body.lower() for w in ["réserver", "reserver", "horaire", "séance", "seance", "billet", "complet"])
        is_imax = "imax" in body.lower()

        if has_sessions or "evenement" in url:
            sessions.append(
                DetectedSession(
                    source="Pathé Direct",
                    date_str="2026-12-16" if "16" in label else "2026-12-15",
                    label=f"{label} : Séances détectées",
                    format_str="IMAX 70mm" if is_imax else "Standard",
                    is_imax=is_imax,
                    url=url,
                )
            )
    except Exception as exc:
        log.warning("[Pathé] Erreur accès direct %s : %s", label, exc)
    return sessions


def check_allocine_api(date_str: str, log: logging.Logger) -> list[DetectedSession]:
    """Vérifie le flux interne AlloCiné (insensible à Akamai)."""
    url = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-{date_str}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(url, headers=headers)
    sessions: list[DetectedSession] = []
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            if res.status != 200:
                return []
            data = json.loads(res.read().decode("utf-8"))
            results = data.get("results", [])
            if not results:
                return []

            for item in results:
                movie = item.get("movie", {})
                title = movie.get("title", "")
                if not is_dune_3(title) and movie.get("internalId") != 324508:
                    continue

                showtimes_dict = item.get("showtimes", {})
                all_st = []
                for k, v in showtimes_dict.items():
                    if isinstance(v, list):
                        all_st.extend(v)

                for st in all_st:
                    starts_at = st.get("startsAt", "")
                    proj = st.get("projection", []) or []
                    tags = st.get("tags", []) or []
                    is_imax = "IMAX" in proj or any("imax" in t.lower() for t in tags)
                    is_70mm = "F_70MM" in proj or any("70mm" in t.lower() for t in tags)

                    pathe_url = ""
                    for t in st.get("data", {}).get("ticketing", []) or []:
                        for u in t.get("urls", []):
                            if "pathe.fr" in u:
                                pathe_url = u
                                break
                        if pathe_url:
                            break

                    if not pathe_url:
                        pathe_url = PATHE_DEC_16_URL if date_str == "2026-12-16" else PATHE_DEC_15_URL

                    time_m = re.search(r"T(\d{2}:\d{2})", starts_at)
                    t_str = time_m.group(1).replace(":", "h") if time_m else starts_at

                    fmt = "IMAX 70mm" if (is_imax and is_70mm) else ("IMAX" if is_imax else "Numérique")
                    sessions.append(
                        DetectedSession(
                            source="AlloCiné (Flux Pathé)",
                            date_str=date_str,
                            label=f"Séance {t_str} - {title}",
                            format_str=fmt,
                            is_imax=is_imax,
                            url=pathe_url,
                        )
                    )
    except Exception as exc:
        log.debug("[AlloCiné] Requête impossible pour %s : %s", date_str, exc)
    return sessions


def run_cycle(p, log: logging.Logger) -> list[DetectedSession]:
    """Exécute un cycle complet de surveillance (Pathé Chrome + AlloCiné)."""
    detected: list[DetectedSession] = []

    # 1. Vérification Pathé en direct via Google Chrome local
    browser = None
    try:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        # Check Sortie 16 décembre
        s16 = check_pathe_via_chrome(page, PATHE_DEC_16_URL, "16 décembre (Sortie)", log)
        detected.extend(s16)

        # Check Avant-première IMAX
        s_imax = check_pathe_via_chrome(page, PATHE_IMAX_EVENT_URL, "Événement IMAX 70mm", log)
        detected.extend(s_imax)

        page.close()
    except Exception as exc:
        log.warning("Vérification Chrome locale indisponible : %s", exc)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    # 2. Vérification immédiate du flux AlloCiné pour le 16 décembre (secours immédiat)
    ac_16 = check_allocine_api("2026-12-16", log)
    if ac_16:
        log.warning("🎯 [AlloCiné] Séances du 16 décembre détectées dans le flux !")
        detected.extend(ac_16)

    return detected


def main():
    parser = argparse.ArgumentParser(description="Moniteur Local 24h/24 Dune 3 Pathé Odysseum")
    parser.add_argument("--interval", type=int, default=60, help="Intervalle en secondes entre deux vérifications (défaut: 60s)")
    parser.add_argument("--once", action="store_true", help="N'exécute qu'une seule vérification et quitte")
    args = parser.parse_args()

    project_dir = Path(__file__).parent
    load_env_file(project_dir / ".env")
    state_file = project_dir / "local_state.json"
    log = setup_logging(project_dir / "local_pathe_monitor.log")

    log.info("==========================================================")
    log.info("🚀 Démarrage du moniteur LOCAL Dune 3 — Pathé Odysseum")
    log.info("📍 Surveillance : 16 décembre (Sortie) & Événement IMAX 70mm")
    log.info("⏱️  Intervalle : %d secondes", args.interval)
    log.info(
        "📱 Canaux : Telegram (%s), ntfy (%s)",
        "Actif" if os.getenv("TELEGRAM_BOT_TOKEN") else "Inactif (configure dans .env)",
        "Actif" if os.getenv("NTFY_TOPIC") else "Inactif (configure dans .env)",
    )
    log.info("==========================================================")

    state = load_state(state_file)
    known_keys = set(state.get("active_keys", []))

    with sync_playwright() as p:
        while True:
            try:
                log.info("--- Cycle de vérification (%s) ---", datetime.now(PARIS).strftime("%H:%M:%S"))
                sessions = run_cycle(p, log)

                fresh = [s for s in sessions if s.key not in known_keys]
                if fresh:
                    imax_first = [s for s in fresh if s.is_imax] or fresh
                    best_url = imax_first[0].url

                    msg = (
                        "🚨 DUNE 3 — BILLETTERIE OUVERTE (DÉTECTION LOCALE PC) !\n\n"
                        "📍 Pathé Montpellier Odysseum\n"
                        "📅 Mercredi 16 décembre 2026\n\n"
                        "🎟️ SÉANCES DISPONIBLES :\n"
                    )
                    for s in fresh:
                        badge = "🌟 IMAX 70mm" if s.is_imax else "🎬"
                        msg += f"• {badge} [{s.format_str}] ({s.source})\n  👉 RÉSERVER : {s.url}\n"

                    msg += (
                        f"\n🔗 Liens directs :\n"
                        f"📅 Page du 16 décembre : {PATHE_DEC_16_URL}\n"
                        f"🎟️ Page IMAX 70mm : {PATHE_IMAX_EVENT_URL}\n"
                    )

                    notify_all(msg, log, title="🚨 DUNE 3 DISPONIBLE (LOCAL) !", click_url=best_url)
                    for s in fresh:
                        known_keys.add(s.key)
                    save_state(state_file, sessions)

            except Exception as exc:
                log.error("Erreur durant le cycle : %s", exc)

            if args.once:
                log.info("Mode --once terminé.")
                break

            time.sleep(args.interval)


if __name__ == "__main__":
    main()
