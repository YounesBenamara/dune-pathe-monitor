@echo off
chcp 65001 > nul
title Moniteur Dune 3 - Pathé Odysseum (Local)

echo ============================================================
echo   🚀 DÉMARRAGE DU MONITEUR DUNE 3 - PATHÉ ODYSSEUM
echo   Surveillance locale 24h/24 (Sortie 16 décembre + IMAX 70mm)
echo ============================================================
echo.

cd /d "%~dp0"

:: Lance la boucle de surveillance (vérification toutes les 60s par défaut)
python local_pathe_monitor.py --interval 60

pause
