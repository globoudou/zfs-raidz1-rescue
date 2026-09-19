#!/usr/bin/env bash
# Lance toute la suite de tests puis re-verifie l'integrite des images sources.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "== Tests unitaires et d'integration =="
python3 -m unittest discover -s tests -t . -v
echo
echo "== Controle forensic post-tests =="
bash scripts/90_verify_readonly.sh
