#!/usr/bin/env python3
"""Test de détection de séance via AlloCiné et via Pathé Événements."""

from __future__ import annotations
import json
import logging
import re
import sys
import urllib.request
from playwright.sync_api import sync_playwright

def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
    return logging.getLogger("test_allocine")

def run():
    log = setup_logging()
    log.info("🧪 Test de récupération des séances Odysseum...")

    # 1. Test via AlloCiné (flux officiel connecté à la billetterie Pathé Odysseum)
    allocine_url = "https://www.allocine.fr/seance/salle_gen_csalle=C0159.html?date=2026-09-18"
    log.info("Interrogation AlloCiné Odysseum (18 sept) : %s", allocine_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        )
        try:
            resp = page.goto(allocine_url, wait_until="domcontentloaded", timeout=20_000)
            log.info("Statut HTTP AlloCiné : %s", resp.status if resp else "None")
            body = page.locator("body").inner_text(timeout=5_000)
            log.info("Titre AlloCiné : %r", page.title())
            if "bound" in body.lower():
                log.info("🎉🎉🎉 BOUND TROUVÉ SUR ALLOCINÉ ODYSSEUM !")
                # Chercher l'horaire et le lien
                for m in re.finditer(r"(bound.*?)(19[h:]45)", body, re.IGNORECASE | re.DOTALL):
                    log.info("Extrait détecté : %r", m.group(0)[:120])
            else:
                log.info("Bound non trouvé dans le texte brut, aperçu : %r", body[:200].replace("\n", " "))
        except Exception as e:
            log.error("Erreur AlloCiné : %s", e)

        # 2. Test direct de l'événement IMAX 70mm sur Pathé
        imax_url = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289"
        log.info("Vérification page IMAX Pathé : %s", imax_url)
        try:
            r_imax = page.goto(imax_url, wait_until="domcontentloaded", timeout=15_000)
            log.info("Statut HTTP page IMAX Pathé : %s (Titre: %r)", r_imax.status if r_imax else "None", page.title())
        except Exception as e:
            log.error("Erreur IMAX Pathé : %s", e)

        browser.close()

if __name__ == "__main__":
    run()
