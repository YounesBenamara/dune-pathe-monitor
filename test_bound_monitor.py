#!/usr/bin/env python3
"""Diagnostic : intercepte tous les appels réseau et endpoints sur pathe.fr."""

from __future__ import annotations
import json
import logging
import re
import sys
from playwright.sync_api import sync_playwright

def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
    return logging.getLogger("diag")

def run():
    log = setup_logging()
    log.info("🔍 Diagnostic réseau Pathé sur GitHub Actions...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            locale="fr-FR",
        )
        page = context.new_page()

        endpoints = []
        page.on("request", lambda req: endpoints.append((req.method, req.resource_type, req.url)))

        log.info("Visite de https://www.pathe.fr/ ...")
        resp = page.goto("https://www.pathe.fr/", wait_until="networkidle", timeout=30_000)
        log.info("Statut HTTP accueil: %s", resp.status if resp else "None")

        log.info("--- Requêtes interceptées (XHR / Fetch / API) ---")
        for method, rtype, url in endpoints:
            if rtype in ("fetch", "xhr") or "api" in url or "pathe" in url:
                log.info("[%s %s] %s", method, rtype, url[:120])

        # Test d'autres routes Pathé : /films, /evenements
        for test_url in [
            "https://www.pathe.fr/films",
            "https://www.pathe.fr/api/cinemas",
            "https://www.pathe.fr/api/shows",
            "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289",
        ]:
            try:
                r = page.goto(test_url, wait_until="domcontentloaded", timeout=10_000)
                log.info("Test route %s -> HTTP %s (Titre: %r)", test_url, r.status if r else "None", page.title())
            except Exception as e:
                log.info("Test route %s -> Erreur %s", test_url, e)

        browser.close()

if __name__ == "__main__":
    run()
