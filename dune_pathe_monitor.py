#!/usr/bin/env python3
"""Surveille la billetterie de Dune 3 à Pathé Odysseum (Montpellier) pour le 16 décembre 2026 via AlloCiné."""

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

PARIS = ZoneInfo("Europe/Paris")

# Configuration des cibles
THEATER_CODE = "P0702"  # Pathé Montpellier Odysseum
TARGET_DATE = "2026-12-16"  # Mercredi 16 décembre 2026 (Sortie nationale)
SECONDARY_DATE = "2026-12-15"  # Mardi 15 décembre 2026 (Avant-première)

ALLOCINE_API_URL = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-{TARGET_DATE}"
ALLOCINE_PUBLIC_URL = f"https://www.allocine.fr/seance/salle_gen_csalle={THEATER_CODE}.html"
ALLOCINE_DUNE3_FILM_URL = "https://www.allocine.fr/film/fichefilm_gen_cfilm=324508.html"

PATHE_DEC_16_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-16"
PATHE_DEC_15_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-12-15"
PATHE_IMAX_EVENT_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289"


@dataclass(frozen=True)
class ShowtimeSession:
    title: str
    date_str: str
    time_str: str
    format_str: str
    language: str
    is_imax: bool
    is_70mm: bool
    pathe_booking_url: str

    @property
    def key(self) -> str:
        raw = f"{self.date_str}|{self.title}|{self.time_str}|{self.format_str}|{self.pathe_booking_url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def setup_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return logging.getLogger("dune_pathe_allocine")


def is_dune_3(text: str) -> bool:
    """Détecte les mentions spécifiques à Dune 3 / Troisième partie."""
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


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_session_keys": []}


def save_state(path: Path, sessions: list[ShowtimeSession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        log.warning("ntfy non configuré (NTFY_TOPIC manquant).")
        return False

    server = (os.getenv("NTFY_SERVER") or "").strip().rstrip("/") or "https://ntfy.sh"
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    priority_map = {
        "min": 1,
        "low": 2,
        "default": 3,
        "high": 4,
        "urgent": 5,
    }
    pri_int = priority_map.get(priority.lower(), 5 if priority == "urgent" else 3)

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
            {
                "action": "view",
                "label": "🎟️ Réserver sur Pathé",
                "url": click_url,
            }
        ]

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "dune-pathe-monitor/2.0",
    }
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
    title: str = "Dune 3 - Pathé Odysseum",
    tags: str = "movie_camera,ticket",
    priority: str = "urgent",
    click_url: str = PATHE_DEC_16_URL,
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


def query_allocine_date(date_str: str, log: logging.Logger) -> tuple[bool, list[ShowtimeSession]]:
    """Interroge l'API interne d'AlloCiné pour le Pathé Odysseum à une date précise."""
    url = f"https://www.allocine.fr/_/showtimes/theater-{THEATER_CODE}/d-{date_str}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": ALLOCINE_PUBLIC_URL,
    }

    req = urllib.request.Request(url, headers=headers)
    retries = 3
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            start_t = time.time()
            with urllib.request.urlopen(req, timeout=12) as res:
                elapsed = time.time() - start_t
                if res.status != 200:
                    log.warning("AlloCiné a retourné le code HTTP %s (tentative %d/%d)", res.status, attempt, retries)
                    time.sleep(1)
                    continue

                raw_data = res.read().decode("utf-8")
                data = json.loads(raw_data)
                log.info("Réponse AlloCiné reçue en %.2fs pour le %s (error=%s)", elapsed, date_str, data.get("error"))

                results = data.get("results", [])
                if not results:
                    log.info("Date %s : Aucun film ni séance dans results (catalogue vide pour ce jour).", date_str)
                    return True, []

                log.info("🎯 Date %s : %d film(s) présent(s) dans results !", date_str, len(results))

                sessions: list[ShowtimeSession] = []
                for item in results:
                    movie = item.get("movie", {})
                    title = movie.get("title", "")
                    internal_id = movie.get("internalId")

                    # Détection Dune 3 ou inclusion générale si des résultats existent ce jour
                    is_dune = is_dune_3(title) or internal_id == 324508

                    showtimes_dict = item.get("showtimes", {})
                    all_st_list = []
                    for k, v in showtimes_dict.items():
                        if isinstance(v, list):
                            all_st_list.extend(v)

                    for st in all_st_list:
                        starts_at = st.get("startsAt", "")
                        projection = st.get("projection", []) or []
                        experience = st.get("experience", []) or []
                        tags = st.get("tags", []) or []
                        diffusion = st.get("diffusionVersion", "")

                        is_imax = "IMAX" in projection or any("imax" in t.lower() for t in tags)
                        is_70mm = "F_70MM" in projection or any("70mm" in t.lower() for t in tags)
                        is_4dx = "E_4DX" in experience or any("4dx" in t.lower() for t in tags)

                        fmts = []
                        if is_imax:
                            fmts.append("IMAX 70mm" if is_70mm else "IMAX")
                        elif is_70mm:
                            fmts.append("70mm")
                        if is_4dx:
                            fmts.append("4DX")
                        if not fmts:
                            fmts.append("Numérique")

                        fmt_str = " + ".join(fmts)
                        lang_str = "VOST" if diffusion == "ORIGINAL" else ("VF" if diffusion == "DUBBED" else diffusion or "VF")

                        # Extraction du lien direct de billetterie Pathé
                        pathe_url = ""
                        ticketing = st.get("data", {}).get("ticketing", []) or []
                        for t in ticketing:
                            for u in t.get("urls", []):
                                if "pathe.fr" in u:
                                    pathe_url = u
                                    break
                            if pathe_url:
                                break

                        # Fallback lien officiel Pathé pour la date si pas de lien spécifique
                        if not pathe_url:
                            pathe_url = PATHE_DEC_16_URL if date_str == TARGET_DATE else PATHE_DEC_15_URL

                        time_m = re.search(r"T(\d{2}:\d{2})", starts_at)
                        time_str = time_m.group(1).replace(":", "h") if time_m else starts_at

                        session = ShowtimeSession(
                            title=title or "Dune : Troisième partie",
                            date_str=date_str,
                            time_str=time_str,
                            format_str=fmt_str,
                            language=lang_str,
                            is_imax=is_imax,
                            is_70mm=is_70mm,
                            pathe_booking_url=pathe_url,
                        )

                        if is_dune or len(results) > 0:
                            sessions.append(session)

                return True, sessions
        except Exception as exc:
            last_err = exc
            log.warning("Erreur connexion AlloCiné (tentative %d/%d) : %s", attempt, retries, exc)
            time.sleep(1)

    log.error("Échec requête AlloCiné après %d tentatives : %s", retries, last_err)
    return False, []


def send_alert_dune_open(sessions: list[ShowtimeSession], date_str: str, log: logging.Logger) -> None:
    """Envoie l'alerte urgente Telegram + ntfy avec mention d'AlloCiné et liens officiels Pathé."""
    # Prioriser les séances IMAX / IMAX 70mm
    imax_sessions = [s for s in sessions if s.is_imax]
    other_sessions = [s for s in sessions if not s.is_imax]
    sorted_sessions = imax_sessions + other_sessions

    # Déterminer l'URL principale pour le clic immédiat
    best_pathe_url = (
        imax_sessions[0].pathe_booking_url
        if imax_sessions
        else (sorted_sessions[0].pathe_booking_url if sorted_sessions else PATHE_DEC_16_URL)
    )

    has_imax = any(s.is_imax for s in sessions)
    header_title = (
        f"🚨 DUNE 3 IMAX 70mm — BILLETTERIE OUVERTE LE 16 DÉCEMBRE !"
        if has_imax
        else f"🚨 DUNE 3 — BILLETTERIE OUVERTE LE 16 DÉCEMBRE !"
    )

    message_lines = [
        header_title,
        "📍 Cinéma : Pathé Montpellier Odysseum",
        f"📅 Date : Mercredi 16 décembre 2026 (Sortie nationale)",
        "📡 Source : Détection instantanée via flux officiel AlloCiné (P0702)",
        "",
        "🎟️ SÉANCES DISPONIBLES :",
    ]

    for s in sorted_sessions:
        badge = "🌟 IMAX 70mm" if (s.is_imax and s.is_70mm) else ("⚡ IMAX" if s.is_imax else "🎬")
        line = f"{badge} [{s.time_str}] {s.format_str} ({s.language})"
        line += f"\n  👉 RÉSERVER SUR PATHÉ : {s.pathe_booking_url}"
        message_lines.append(line)

    message_lines.extend(
        [
            "",
            "🔗 LIENS OFFICIELS :",
            f"📅 Page Pathé du 16 décembre :\n{PATHE_DEC_16_URL}",
            f"🎟️ Événement Pathé IMAX 70mm :\n{PATHE_IMAX_EVENT_URL}",
            f"🎬 Fiche film AlloCiné :\n{ALLOCINE_DUNE3_FILM_URL}",
            "",
            "⚡ Clique immédiatement sur le lien Pathé le plus haut pour choisir tes sièges !",
        ]
    )

    full_message = "\n".join(message_lines)
    log.warning(full_message)

    notify_all(
        full_message,
        log,
        title=f"🚨 DUNE 3 LE 16 DÉCEMBRE {'(IMAX)' if has_imax else ''} — PATHÉ ODYSSEUM",
        tags="rotating_light,ticket,popcorn",
        priority="urgent",
        click_url=best_pathe_url,
    )


def check(
    target_date: str = TARGET_DATE,
    data_dir: Path = Path(__file__).parent,
    force_notify: bool = False,
) -> int:
    state_file = data_dir / "dune_pathe_state.json"
    log = setup_logging(data_dir / "dune_pathe_monitor.log")

    log.info("=== Vérification AlloCiné — Pathé Odysseum (Montpellier) pour le %s ===", target_date)
    success, sessions = query_allocine_date(target_date, log)

    if not success:
        log.error("Impossible de joindre AlloCiné à cette exécution.")
        return 1

    state = load_state(state_file)
    known_keys = set(state.get("active_session_keys", []))

    if force_notify:
        log.info("Option --force-notify activée : Envoi immédiat d'une notification de test.")
        test_msg = (
            "🧪 [TEST MANUEL] Moniteur Dune 3 — AlloCiné & Pathé Odysseum\n\n"
            "✅ Vos notifications Telegram et ntfy fonctionnent parfaitement !\n\n"
            f"📡 Vérification effectuée via l'API AlloCiné (P0702) pour le {target_date}.\n"
            f"Statut : results contient actuellement {len(sessions)} séance(s).\n"
            "Dès qu'une séance pour Dune 3 apparaît, l'alerte immédiate sera envoyée avec les liens officiels de réservation Pathé (avec priorité IMAX).\n\n"
            "🔗 Liens officiels Pathé :\n"
            f"📅 Sortie 16 décembre :\n{PATHE_DEC_16_URL}\n\n"
            f"🎟️ Événement IMAX 70mm :\n{PATHE_IMAX_EVENT_URL}\n\n"
            f"🎬 Fiche AlloCiné :\n{ALLOCINE_DUNE3_FILM_URL}"
        )
        notify_all(
            test_msg,
            log,
            title="[TEST] Moniteur Dune 3 - AlloCine / Pathe Odysseum",
            tags="test_tube,white_check_mark",
            priority="default",
            click_url=PATHE_DEC_16_URL,
        )
        if sessions:
            send_alert_dune_open(sessions, target_date, log)
        return 0

    if sessions:
        fresh_sessions = [s for s in sessions if s.key not in known_keys]
        if fresh_sessions:
            log.warning("🚨 NOUVELLE(S) SÉANCE(S) DÉTECTÉE(S) : %d séance(s) !", len(fresh_sessions))
            send_alert_dune_open(fresh_sessions, target_date, log)
            for s in fresh_sessions:
                known_keys.add(s.key)
            save_state(state_file, sessions)
        else:
            log.info("Séance(s) déjà connue(s) (%d séance(s)) : pas de doublon.", len(sessions))
    else:
        log.info("results est vide pour le %s : les réservations ne sont pas encore ouvertes.", target_date)
        save_state(state_file, [])

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Moniteur Dune 3 / Pathé Odysseum via AlloCiné")
    parser.add_argument("--date", type=str, default=TARGET_DATE, help="Date cible (YYYY-MM-DD)")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--force-notify", action="store_true", help="Force l'envoi d'une notification de test")
    args = parser.parse_args()
    return check(args.date, args.data_dir, args.force_notify)


if __name__ == "__main__":
    raise SystemExit(main())
