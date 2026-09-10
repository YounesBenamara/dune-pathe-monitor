import os
import sys
import urllib.request
import urllib.error
import json

# Récupère le token soit depuis la variable d'environnement GITHUB_TOKEN, soit en argument CLI
TOKEN = os.environ.get("GITHUB_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else None)

if not TOKEN:
    print("Usage: python trigger_monitor.py <VOTRE_PAT_GITHUB>")
    print("Ou définir la variable d'environnement GITHUB_TOKEN")
    sys.exit(1)

url = "https://api.github.com/repos/YounesBenamara/dune-pathe-monitor/actions/workflows/dune-monitor.yml/dispatches"
payload = json.dumps({"ref": "main"}).encode("utf-8")

headers = {
    "Authorization": f"Bearer {TOKEN.strip()}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "dune-monitor-trigger",
    "Content-Type": "application/json"
}

req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

try:
    with urllib.request.urlopen(req) as resp:
        if resp.status in (200, 204):
            print("🚀 Succès (HTTP 204) : Le workflow a été déclenché instantanément sur GitHub Actions !")
        else:
            print(f"Status: {resp.status}")
except urllib.error.HTTPError as e:
    print(f"❌ Erreur HTTP {e.code}: {e.read().decode('utf-8')}")
except Exception as e:
    print(f"❌ Erreur: {e}")
