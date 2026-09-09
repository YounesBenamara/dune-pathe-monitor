# Exécution dans le cloud, PC éteint

Ce dossier est prêt à devenir un dépôt GitHub. Une fois importé, GitHub Actions
exécutera le moniteur toutes les 30 minutes, toute l'année et dès maintenant.
Il garde son état via le cache GitHub, donc Telegram n'est
prévenu que lorsqu'une nouvelle trace de séance apparaît.

## Mise en ligne

1. Créer un dépôt GitHub vide nommé par exemple `dune-pathe-monitor`.
2. Importer **le contenu** de ce dossier `outputs` à la racine du dépôt :
   `dune_pathe_monitor.py`, `requirements.txt` et le dossier caché `.github`.
3. Dans GitHub, ouvrir **Settings → Secrets and variables → Actions** puis créer :
   - `TELEGRAM_BOT_TOKEN` : le jeton de ton bot Telegram ;
   - `TELEGRAM_CHAT_ID` : l'identifiant du chat qui recevra l'alerte.
4. Dans l'onglet **Actions**, ouvrir « Surveiller Dune 3 — Pathé Odysseum » et
   lancer **Run workflow** une première fois pour vérifier Telegram.

Ne mets jamais le jeton Telegram dans un fichier du dépôt : il doit rester dans
les deux secrets GitHub.

## Gratuité et limite importante

Un dépôt public utilisant les runners GitHub standards est gratuit. Sur un
compte GitHub Free, un dépôt privé bénéficie d'un quota mensuel de 2 000 minutes.
Une exécution avec installation complète de Chromium peut être assez coûteuse
en minutes ; un dépôt public est donc la solution gratuite la plus simple.

GitHub peut retarder, et exceptionnellement manquer, un lancement planifié lors
de fortes charges. Ce n'est pas un système temps réel garanti. Pour une
surveillance réellement continue à la minute près, il faudrait un hébergement
payant ou un serveur personnel toujours allumé.

