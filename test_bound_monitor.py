#!/usr/bin/env python3
"""Vérification d'AlloCiné pour Pathé Odysseum (P0702) le 18 septembre 2026."""

from __future__ import annotations
import logging
import re
import sys
from playwright.sync_api import sync_playwright

def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
    return logging.getLogger("check_allocine")

def run():
    log = setup_logging()
    # Code AlloCiné de Pathé Odysseum : P0702
    url = "https://www.allocine.fr/seance/salle_gen_csalle=P0702.html?date=2026-09-18"
    log.info("Interrogation AlloCiné Pathé Odysseum (18 sept) : %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        )
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            log.info("Statut HTTP : %s", resp.status if resp else "None")
            title = page.title()
            log.info("Titre page : %r", title)
            body = page.locator("body").inner_text(timeout=5_000)
            
            # Vérifier si on est bien sur Odysseum
            if "odysseum" in body.lower() or "méliès" in body.lower() or "multiplexe" in body.lower():
                log.info("🎯 Confirmation : on est bien sur la page de Pathé Odysseum Montpellier !")
            
            # Vérifier si Bound et 19:45 sont présents
            if "bound" in body.lower():
                log.info("🎉🎉🎉 BOUND EST PRÉSENT SUR LA GRILLE D'ODYSSEUM !")
                match = re.search(r"bound.{0,100}19[h:]45", body, re.IGNORECASE | re.DOTALL)
                if match:
                    log.info("🎯 Séance exacte trouvée : %r", match.group(0))
            else:
                log.warning("Bound non trouvé dans le texte rendu. Aperçu : %r", body[:200].replace("\n", " "))
        except Exception as e:
            log.error("Erreur : %s", e)
        finally:
            browser.close()

if __name__ == "__main__":
    run()
