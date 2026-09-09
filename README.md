# Exécution dans le cloud, PC éteint

Ce dossier est prêt pour GitHub Actions. Une fois configuré, GitHub Actions
exécutera le moniteur **chaque jour** pour surveiller l'ouverture des préventes
et séances de **Dune 3** au cinéma **Pathé Odysseum** à Montpellier.
L'état est persisté via le cache GitHub, évitant les alertes en doublon :
vous n'êtes notifié que lorsqu'une nouvelle séance ou mention apparaît.

## Mise en ligne

1. Créer un dépôt GitHub (public ou privé) nommé par exemple `dune-pathe-monitor`.
2. Pousser le code de ce dossier à la racine du dépôt :
   `dune_pathe_monitor.py`, `requirements.txt` et `.github/workflows/dune-monitor.yml`.
3. Dans GitHub, aller dans **Settings → Secrets and variables → Actions** puis créer ces secrets :
   - `TELEGRAM_BOT_TOKEN` : le jeton de votre bot Telegram (obtenu via @BotFather) ;
   - `TELEGRAM_CHAT_ID` : l'identifiant de votre chat / groupe Telegram ;
   - `NTFY_TOPIC` : le nom de votre sujet ntfy (ex: `dune3-odysseum-alerte-unique-xyz`) ;
   - `NTFY_TOKEN` : *(facultatif)* uniquement si votre topic ntfy requiert une authentification.
4. Dans l'onglet **Actions**, ouvrir « Surveiller Dune 3 — Pathé Odysseum » et
   lancer **Run workflow** pour tester manuellement le moniteur.

Ne mettez jamais vos jetons dans un fichier du dépôt : ils doivent rester dans les secrets GitHub.

## Notifications

- **Telegram** : envoie un message direct dès qu'une séance ou mention est repérée avec lien vers le cinéma.
- **ntfy** : notification push instantanée sur mobile/bureau via l'application ntfy ou `ntfy.sh/<topic>`.

## Quotas et fréquence

- Le workflow est configuré pour s'exécuter **une fois par jour** (à 07:15 UTC / 09:15 heure de Paris en été).
- Cette fréquence ne consomme qu'environ **30 à 60 minutes par mois**, largement en dessous du quota gratuit de **2 000 minutes par mois** de GitHub Actions sur les dépôts privés (et 100% gratuit en illimité sur les dépôts publics).

