#!/usr/bin/env python3
"""Script de test dédié : surveille le film Bound au Pathé Odysseum le 18 septembre 2026."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BOUND_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-09-18"
CINEMA_URL = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum"
PARIS = ZoneInfo("Europe/Paris")

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR', 'fr', 'en-US', 'en'] });
"""


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


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return logging.getLogger("test_bound")


def notify_telegram(message: str, log: logging.Logger) -> bool:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        log.warning("Telegram non configuré.")
        return False
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception as exc:
        log.error("Erreur envoi Telegram : %s", exc)
        return False


def notify_ntfy(message: str, log: logging.Logger, title: str, click_url: str = BOUND_URL) -> bool:
    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        log.warning("ntfy non configuré.")
        return False

    server = (os.getenv("NTFY_SERVER") or "").strip().rstrip("/") or "https://ntfy.sh"
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    payload_data = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 5,
        "tags": ["rotating_light", "ticket", "cinema"],
        "click": click_url,
        "actions": [
            {
                "action": "view",
                "label": "Ouvrir la séance",
                "url": click_url,
            }
        ],
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "bound-test-monitor/1.0",
    }
    token = (os.getenv("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(
            server,
            data=json.dumps(payload_data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.error("Erreur envoi ntfy : %s", exc)
        return False


def notify_all(message: str, log: logging.Logger, title: str, click_url: str = BOUND_URL) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_tg = executor.submit(notify_telegram, message, log)
        f_ntfy = executor.submit(notify_ntfy, message, log, title, click_url)
        res_tg = f_tg.result()
        res_ntfy = f_ntfy.result()

    channels = []
    if res_tg:
        channels.append("Telegram")
    if res_ntfy:
        channels.append("ntfy")
    log.info("Canaux notifiés : %s", ", ".join(channels) or "aucun")


def is_valid_showtime(text: str) -> bool:
    match = re.search(r"\b(0?[0-9]|1[0-9]|2[0-3])[:h]([0-5][0-9])\b", text)
    if not match:
        return False
    hour = int(match.group(1))
    return 9 <= hour <= 23 or hour <= 1


def is_bound(text: str) -> bool:
    return bool(re.search(r"\bbound\b", text, re.IGNORECASE))


def dismiss_overlays(page) -> None:
    for selector in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Tout accepter')",
        "button:has-text('Continuer sans accepter')",
        "button:has-text('Accepter')",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=800)
                page.wait_for_timeout(300)
                break
        except Exception:
            pass


def extract_bound_sessions(page, context_label: str = "ven. 18 sept.") -> list[Session]:
    results: list[Session] = []
    seen: set[str] = set()

    cards = page.locator(
        "article, [class*='movie'], [class*='film'], [class*='schedule'], [class*='card'], [data-testid*='movie']"
    ).all()

    matched_cards = []
    for card in cards:
        try:
            card_text = normalise(card.inner_text(timeout=1_500))
        except Exception:
            continue
        if is_bound(card_text):
            matched_cards.append((card, card_text))

    if not matched_cards:
        try:
            nodes = page.get_by_text(re.compile(r"\bbound\b", re.IGNORECASE)).all()
            for node in nodes[:3]:
                parent = node.locator("xpath=ancestor::*[self::article or self::section or contains(@class,'card') or contains(@class,'movie') or self::li][1]")
                if parent.count() > 0:
                    matched_cards.append((parent.first, normalise(parent.first.inner_text(timeout=1_500))))
        except Exception:
            pass

    for card, card_text in matched_cards:
        elements = card.locator(
            "button, a, time, [class*='session'], [class*='showtime'], [class*='slot'], [class*='complet']"
        ).all()

        for el in elements:
            try:
                text = normalise(el.inner_text(timeout=800))
                if not text or len(text) > 50:
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

    if not results and matched_cards:
        card, card_text = matched_cards[0]
        href = ""
        try:
            links = card.locator("a[href]").all()
            for l in links:
                h = l.get_attribute("href") or ""
                if h and not h.startswith("#"):
                    href = urllib.parse.urljoin("https://www.pathe.fr", h)
                    break
        except Exception:
            pass
        results.append(Session(context_label, f"Film Bound au programme ({card_text[:80]}...)", url=href))

    return results


def check_bound() -> int:
    log = setup_logging()
    log.info("🧪 [TEST BOUND] Début de la vérification pour le film BOUND le 18 septembre 2026...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )

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
                "Sec-Ch-Ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

        # 1. Chargement de l'accueil pour session Akamai valide (HTTP 200)
        log.info("1. Initialisation sur https://www.pathe.fr/ ...")
        try:
            resp = page.goto("https://www.pathe.fr/", wait_until="domcontentloaded", timeout=25_000)
            log.info("Accueil HTTP : %s", resp.status if resp else "None")
        except Exception as e:
            log.warning("Erreur accueil : %s", e)

        dismiss_overlays(page)

        # Simulation de présence humaine pour validation Akamai telemetry
        page.mouse.move(250, 250)
        page.wait_for_timeout(500)
        page.mouse.move(500, 300)
        page.wait_for_timeout(1_000)

        # 2. Navigation vers la page Odysseum 18 sept
        log.info("2. Navigation vers Odysseum (18 sept)...")
        nav_ok = False
        try:
            page.evaluate("""() => {
                const a = document.createElement('a');
                a.href = '/cinemas/cinema-pathe-odysseum/filters/date-2026-09-18';
                a.id = 'link-direct-bound';
                a.innerText = 'Test Bound';
                document.body.appendChild(a);
            }""")
            with page.expect_navigation(timeout=15_000):
                page.click("#link-direct-bound")
            nav_ok = True
            log.info("Navigation interne par clic réussie : %s", page.url)
        except Exception as e:
            log.info("Transition par clic interne non terminée (%s), tentative goto direct avec referer...", e)

        if not nav_ok or "403" in page.title() or "Allo Houston" in normalise(page.locator("body").inner_text(timeout=2_000)):
            try:
                page.goto(
                    BOUND_URL,
                    referer="https://www.pathe.fr/",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
            except Exception as e:
                log.warning("Erreur goto BOUND_URL : %s", e)

        dismiss_overlays(page)
        page.wait_for_timeout(2_000)

        body = normalise(page.locator("body").inner_text(timeout=5_000))
        title = page.title()
        log.info("Page Odysseum chargée : URL=%s, Titre=%r, Aperçu=%r", page.url, title, body[:120])

        # 3. Si on est sur la page Odysseum mais que la date 18 sept n'est pas sélectionnée, cliquer l'onglet "18"
        if not is_bound(body) and "odysseum" in page.url.lower():
            log.info("Recherche du bouton '18 sept.' dans le carrousel des dates...")
            date_btns = page.locator("button, a, [role='tab'], [class*='date'], [class*='day']").all()
            for btn in date_btns:
                try:
                    bt = normalise(btn.inner_text(timeout=500))
                    if ("18" in bt and ("sept" in bt.lower() or "ven" in bt.lower())) or bt == "18":
                        log.info("Bouton date 18 trouvé : %r -> Clic !", bt)
                        btn.click()
                        page.wait_for_timeout(3_000)
                        body = normalise(page.locator("body").inner_text(timeout=5_000))
                        break
                except Exception:
                    continue

        if "Allo Houston" in body:
            log.error("❌ Page toujours bloquée par Akamai (Allo Houston).")
            return 1

        if not is_bound(body):
            log.warning("❌ 'Bound' non détecté sur la page du 18 septembre.")
            test_msg = (
                "🧪 [TEST BOUND] Moniteur Pathé Odysseum\n\n"
                "ℹ️ La page du cinéma Odysseum est accessible (non bloquée) mais Bound n'a pas été détecté.\n"
                f"📅 Lien direct :\n{BOUND_URL}"
            )
            notify_all(test_msg, log, title="[TEST] Bound non trouvé", click_url=BOUND_URL)
            browser.close()
            return 0

        log.info("🎯 'BOUND' DÉTECTÉ SUR LA PAGE ! Extraction de la séance...")
        sessions = extract_bound_sessions(page, "ven. 18 sept.")
        log.info("%d séance(s) trouvée(s) pour Bound.", len(sessions))

        # Construction du message d'alerte
        message = "🚨 BOUND – SÉANCE DISPONIBLE AU PATHÉ ODYSSEUM !\n(ven. 18 sept.)\n\n"
        lines = []
        first_res_url = ""
        for s in sessions:
            item = f"• [{s.date_label}] {s.text}"
            if s.url:
                item += f"\n  👉 RÉSERVER ICI : {s.url}"
                if not first_res_url:
                    first_res_url = s.url
            lines.append(item)

        if not lines:
            lines.append("• [ven. 18 sept.] Séance programmée détectée sur la grille")

        message += "\n".join(lines)
        message += (
            f"\n\n📅 Lien direct séance :\n{BOUND_URL}\n\n"
            "⚡ Ouvre le lien pour réserver !"
        )

        click_url = first_res_url or BOUND_URL
        log.warning(message)
        notify_all(
            message,
            log,
            title="🚨 BOUND DISPONIBLE – Pathé Odysseum",
            click_url=click_url,
        )

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(check_bound())
