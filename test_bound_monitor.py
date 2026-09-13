#!/usr/bin/env python3
"""Test de connexion Playwright Chromium via proxy Webshare sur GitHub Actions."""

from __future__ import annotations
import logging
import re
import sys
from playwright.sync_api import sync_playwright

def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
    return logging.getLogger("test_proxy")

def run():
    log = setup_logging()
    log.info("🧪 Test Playwright avec Proxy Webshare...")

    proxy_server = "http://31.59.20.176:6754"
    proxy_user = "jspepmxl"
    proxy_pass = "wyycy4hqi6d7"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            proxy={
                "server": proxy_server,
                "username": proxy_user,
                "password": proxy_pass,
            }
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            locale="fr-FR",
        )
        page = context.new_page()

        # 1. Vérification de l'IP du proxy
        try:
            page.goto("https://api.ipify.org?format=json", timeout=15_000)
            ip_info = page.locator("body").inner_text()
            log.info("IP vue par le web via proxy : %s", ip_info)
        except Exception as e:
            log.error("Erreur vérification IP proxy : %s", e)

        # 2. Test direct de la page Pathé Odysseum (18 sept)
        target_url = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum/filters/date-2026-09-18"
        log.info("Chargement de : %s", target_url)
        try:
            resp = page.goto(target_url, wait_until="domcontentloaded", timeout=25_000)
            log.info("Code HTTP retourné : %s", resp.status if resp else "None")
            title = page.title()
            body = page.locator("body").inner_text(timeout=5_000)
            log.info("Titre : %r", title)
            log.info("Aperçu body (150 car) : %r", body[:150].replace("\n", " "))
            if "bound" in body.lower():
                log.info("🎉🎉🎉 SUCCÈS TOTAL : LE FILM BOUND EST DÉTECTÉ VIA LE PROXY !")
            elif "Allo Houston" in body:
                log.warning("❌ Akamai a bloqué cette IP de proxy (Allo Houston).")
            else:
                log.info("ℹ️ Page chargée sans blocage, statut Bound : %s", "présent" if "bound" in body.lower() else "non trouvé")
        except Exception as e:
            log.error("Erreur chargement Pathé : %s", e)

        browser.close()

if __name__ == "__main__":
    run()
