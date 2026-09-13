#!/usr/bin/env python3
"""Moniteur LOCAL permanent pour Dune 3 à Pathé Odysseum (Montpellier).

Système en cascade prioritaire (if / elif / elif / else) sans redondance pour le 16 décembre :
1. IF : Page Événement IMAX 70mm (16 décembre) sur pathe.fr
   -> Si séance détectée : ALERTE IMMÉDIATE et arrêt du cycle (pas de doublon).
2. ELIF : Page Cinéma Pathé Odysseum (16 décembre) sur pathe.fr
   -> Si séance détectée : ALERTE IMMÉDIATE et arrêt du cycle.
3. ELIF : Flux officiel AlloCiné pour Pathé Odysseum (16 décembre)
   -> Si séance détectée : ALERTE IMMÉDIATE avec liens officiels Pathé et arrêt du cycle.
4. ELSE :
   -> Aucune séance ouverte pour le moment, attente du prochain cycle.
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

# URLs ciblées pour le 16 décembre 2026 (Sortie nationale)
IMAX_16_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289/filters/date-2026-12-16"
PATHE_FILM_16_URL = "https://www.pathe.fr/films/dune-troisieme-partie-50828/filters/date-2026-12-16"
PATHE_FILM_URL = "https://www.pathe.fr/films/dune-troisieme-partie-50828"
PATHE_DEC_16_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-16"

# Flux AlloCiné (P0702 = Pathé Odysseum)
THEATER_CODE = "P0702"
ALLOCINE_16_API = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-2026-12-16"

# Détection automatique de Brave Browser
BRAVE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]

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


def get_browser_executable() -> tuple[str | None, str]:
    for p in BRAVE_PATHS:
        if os.path.isfile(p):
            return p, "Brave"
    return None, "Chrome"


def load_env_file(env_path: Path) -> None:
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
    click_url: str = IMAX_16_URL,
) -> bool:
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
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
    click_url: str = IMAX_16_URL,
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


def extract_time_slots(page) -> list[dict]:
    """Extrait uniquement les vrais créneaux horaires de séances pour éviter tout faux positif."""
    slots = []
    elements = page.locator("button, a, time, [class*='slot'], [class*='session'], [class*='showtime']").all()
    for el in elements:
        try:
            txt = el.inner_text().strip()
            # Horaire réel (ex: 20h15, 19:45)
            m = re.search(r"\b(0?[9]|1[0-9]|2[0-3])[:h]([0-5][0-9])\b", txt)
            if not m:
                continue

            href = el.get_attribute("href") or ""
            if not href:
                parent = el.locator("xpath=ancestor-or-self::a").first
                if parent.count() > 0:
                    href = parent.get_attribute("href") or ""

            context_text = ""
            try:
                card = el.locator("xpath=ancestor::*[contains(@class, 'card') or contains(@class, 'movie') or contains(@class, 'schedule')][1]").first
                if card.count() > 0:
                    context_text = card.inner_text()
            except Exception:
                pass

            slots.append({
                "time": m.group(0),
                "href": href,
                "context": context_text,
                "is_imax": "imax" in (txt + context_text).lower(),
            })
        except Exception:
            continue
    return slots


def check_pathe_page(page, url: str, label: str, log: logging.Logger) -> list[DetectedSession]:
    """Charge une page Pathé et extrait les séances avec créneaux horaires."""
    sessions: list[DetectedSession] = []
    try:
        t0 = time.time()
        res = page.goto(url, wait_until="domcontentloaded", timeout=12000)
        elapsed = (time.time() - t0) * 1000
        status = res.status if res else 0

        # Fermer cookies si présents
        for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accepter')"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=300):
                    btn.click(timeout=300)
                    break
            except Exception:
                pass

        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = page.content()

        title = page.title()

        if "Allo Houston" in body or status == 403:
            log.warning("[Pathé] %s : Akamai 403 (Allo Houston) [%.0fms]", label, elapsed)
            return []

        if not is_dune_3(body) and not is_dune_3(title):
            log.info("[Pathé] %s : HTTP %s [%.0fms] — Aucune séance ouverte.", label, status, elapsed)
            return []

        # Vérification stricte des créneaux
        slots = extract_time_slots(page)
        if not slots:
            log.info(
                "[Pathé] %s : HTTP %s [%.0fms] — Page chargée, Dune 3 mentionné, mais zéro séance horaire pour le moment.",
                label,
                status,
                elapsed,
            )
            return []

        log.warning("🚨 [Pathé] SÉANCE(S) HORAIRE(S) OUVERTE(S) SUR %s : %d séance(s) !", label, len(slots))
        for slot in slots:
            booking_url = slot["href"] or url
            if booking_url.startswith("/"):
                booking_url = f"https://www.pathe.fr{booking_url}"
            sessions.append(
                DetectedSession(
                    source="Pathé Officiel",
                    date_str="2026-12-16",
                    label=f"Séance {slot['time']} ({label})",
                    format_str="IMAX 70mm" if slot["is_imax"] else "Standard",
                    is_imax=slot["is_imax"],
                    url=booking_url,
                )
            )
    except Exception as exc:
        log.warning("[Pathé] Requête %s : %s", label, exc)
    return sessions


def check_allocine_16(page, log: logging.Logger) -> list[DetectedSession]:
    """Vérifie l'API interne AlloCiné pour le 16 décembre (P0702)."""
    sessions: list[DetectedSession] = []
    try:
        t0 = time.time()
        res = page.goto(ALLOCINE_16_API, wait_until="domcontentloaded", timeout=10000)
        elapsed = (time.time() - t0) * 1000
        if not res or res.status != 200:
            return []

        content = page.locator("body").inner_text(timeout=2000)
        data = json.loads(content)
        results = data.get("results", [])

        if not results:
            log.info("[AlloCiné] 16 décembre : HTTP 200 [%.0fms] — results vide (pas de séance).", elapsed)
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

            if not all_st:
                continue

            log.warning("🚨 [AlloCiné] SÉANCES 16 DÉCEMBRE OUVERTES : %s (%d séances) !", title, len(all_st))

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
                    pathe_url = IMAX_16_URL

                time_m = re.search(r"T(\d{2}:\d{2})", starts_at)
                t_str = time_m.group(1).replace(":", "h") if time_m else starts_at

                fmt = "IMAX 70mm" if (is_imax and is_70mm) else ("IMAX" if is_imax else "Numérique")
                sessions.append(
                    DetectedSession(
                        source="AlloCiné (Flux Pathé)",
                        date_str="2026-12-16",
                        label=f"Séance {t_str} - {title}",
                        format_str=fmt,
                        is_imax=is_imax,
                        url=pathe_url,
                    )
                )
    except Exception as exc:
        log.warning("[AlloCiné] Erreur flux : %s", exc)
    return sessions


def run_cycle_if_else(p, log: logging.Logger) -> tuple[str, list[DetectedSession]]:
    """
    Système en cascade IF / ELIF / ELIF / ELSE sans redondance pour le 16 décembre :
    - IF : Page Événement IMAX 70mm Pathé -> Si séances, alerte et STOP.
    - ELIF : Page Cinéma Pathé Odysseum -> Si séances, alerte et STOP.
    - ELIF : Flux officiel AlloCiné -> Si séances, alerte et STOP.
    - ELSE : Aucune séance, fin de cycle.
    """
    browser_exe, _ = get_browser_executable()
    browser = None
    try:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--host-resolver-rules=MAP www.pathe.fr 2.18.64.34, MAP pathe.fr 2.18.64.34, MAP *.pathe.fr 2.18.64.34, MAP www.allocine.fr 104.18.39.231",
            ],
        }
        if browser_exe:
            launch_kwargs["executable_path"] = browser_exe
        else:
            launch_kwargs["channel"] = "chrome"

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        # =========================================================================
        # 1. IF : Vérification Page Événement IMAX 70mm (16 décembre)
        # =========================================================================
        sessions_imax = check_pathe_page(page, IMAX_16_URL, "IMAX 70mm (16 déc.)", log)
        if sessions_imax:
            log.info("🎯 Trouvé sur la page Événement IMAX 70mm ! Pas besoin de vérifier le reste.")
            page.close()
            return "IMAX 70mm", sessions_imax

        # =========================================================================
        # 2. ELIF : Vérification Fiche Film Officielle Dune 3 (16 décembre)
        # =========================================================================
        sessions_film = check_pathe_page(page, PATHE_FILM_16_URL, "Fiche Film Dune 3 (16 déc.)", log)
        if sessions_film:
            log.info("🎯 Trouvé sur la fiche officielle du film ! Pas besoin de vérifier le reste.")
            page.close()
            return "Fiche Film Pathé", sessions_film

        # =========================================================================
        # 3. ELIF : Vérification Page Cinéma Pathé Odysseum (16 décembre)
        # =========================================================================
        sessions_cinema = check_pathe_page(page, PATHE_DEC_16_URL, "Cinéma Pathé Odysseum (16 déc.)", log)
        if sessions_cinema:
            log.info("🎯 Trouvé sur la page Cinéma Odysseum ! Pas besoin de vérifier le reste.")
            page.close()
            return "Cinéma Odysseum", sessions_cinema

        # =========================================================================
        # 3. ELIF : Vérification Flux AlloCiné pour le 16 décembre (Secours direct)
        # =========================================================================
        sessions_ac = check_allocine_16(page, log)
        if sessions_ac:
            log.info("🎯 Trouvé sur le flux AlloCiné ! Liens officiels Pathé récupérés.")
            page.close()
            return "AlloCiné", sessions_ac

        # =========================================================================
        # 4. ELSE : Rien n'est encore ouvert
        # =========================================================================
        page.close()
        return "Aucune", []

    except Exception as exc:
        log.warning("Erreur technique durant le cycle : %s", exc)
        return "Erreur", []
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Moniteur Local 24h/24 Dune 3 Pathé Odysseum")
    parser.add_argument("--interval", type=int, default=60, help="Intervalle en secondes entre vérifications (défaut: 60s)")
    parser.add_argument("--once", action="store_true", help="N'exécute qu'un seul cycle et quitte")
    args = parser.parse_args()

    project_dir = Path(__file__).parent
    load_env_file(project_dir / ".env")
    state_file = project_dir / "local_state.json"
    log = setup_logging(project_dir / "local_pathe_monitor.log")

    browser_exe, browser_name = get_browser_executable()

    log.info("==========================================================")
    log.info("🚀 Démarrage du moniteur LOCAL Dune 3 — 16 Décembre 2026")
    log.info("🌐 Moteur : %s (%s)", browser_name, browser_exe or "défaut")
    log.info("📐 Logique : Système en cascade IF/ELIF/ELSE sans redondance")
    log.info("   1. IF   : Page Événement IMAX 70mm (16 déc.)")
    log.info("   2. ELIF : Fiche Film Officielle Dune 3 (16 déc.)")
    log.info("   3. ELIF : Page Cinéma Odysseum (16 déc.)")
    log.info("   4. ELIF : Flux officiel AlloCiné (16 déc.)")
    log.info("⏱️  Intervalle : %d secondes", args.interval)
    log.info("📱 Canaux : Telegram (%s), ntfy (%s)", "Actif" if os.getenv("TELEGRAM_BOT_TOKEN") else "Inactif", "Actif" if os.getenv("NTFY_TOPIC") else "Inactif")
    log.info("==========================================================")

    state = load_state(state_file)
    known_keys = set(state.get("active_keys", []))

    with sync_playwright() as p:
        while True:
            try:
                log.info("--- Cycle de vérification (%s) ---", datetime.now(PARIS).strftime("%H:%M:%S"))
                source_found, sessions = run_cycle_if_else(p, log)

                if sessions:
                    fresh = [s for s in sessions if s.key not in known_keys]
                    if fresh:
                        imax_first = [s for s in fresh if s.is_imax] or fresh
                        best_url = imax_first[0].url

                        has_imax = any(s.is_imax for s in fresh)
                        header = "🚨 DUNE 3 IMAX 70mm — BILLETTERIE OUVERTE !" if has_imax else "🚨 DUNE 3 — BILLETTERIE OUVERTE !"

                        msg = (
                            f"{header}\n\n"
                            "📍 Pathé Montpellier Odysseum\n"
                            "📅 Mercredi 16 décembre 2026\n"
                            f"📡 Source : Détection via {source_found}\n\n"
                            "🎟️ SÉANCES DISPONIBLES :\n"
                        )
                        for s in fresh:
                            badge = "🌟 IMAX 70mm" if s.is_imax else "🎬"
                            msg += f"• {badge} [{s.format_str}]\n  👉 RÉSERVER : {s.url}\n"

                        msg += (
                            f"\n🔗 Liens officiels :\n"
                            f"📅 Sortie 16 décembre : {PATHE_DEC_16_URL}\n"
                            f"🎟️ Événement IMAX 70mm : {IMAX_16_URL}\n"
                        )

                        notify_all(msg, log, title=header, click_url=best_url)
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
