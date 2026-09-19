#!/usr/bin/env bash
# =============================================================================
# 85_paquet_publication.sh — prepare le depot pour publication
#
# 1. anonymise rapports et documentation ;
# 2. verifie qu'il ne reste aucun element identifiant ;
# 3. lance la suite de tests ;
# 4. produit un « git bundle » lisible par un autre compte de la machine.
#
#   bash scripts/85_paquet_publication.sh [chemin/du/paquet.bundle]
# =============================================================================
set -euo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAQUET="${1:-/tmp/zfs-raidz1-rescue.bundle}"
cd "$PROJ"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "Anonymisation"
python3 scripts/80_anonymiser.py
python3 scripts/80_anonymiser.py --check

log "Tests"
python3 -m unittest discover -s tests -t . 2>&1 | tail -3

log "Etat du depot"
git status --short || true
if [[ -n "$(git status --porcelain)" ]]; then
  echo
  echo "Des modifications ne sont pas encore validees. Faites par exemple :"
  echo "    git add -A && git commit -m \"...\""
  exit 1
fi

log "Paquet de transfert"
git bundle create "$PAQUET" --all
chmod 644 "$PAQUET"
git bundle verify "$PAQUET" | tail -2
ls -l "$PAQUET"

cat <<TXT

A executer depuis VOTRE session (celle qui a vos identifiants GitHub) :

    git clone $PAQUET ~/zfs-raidz1-rescue
    cd ~/zfs-raidz1-rescue
    git remote remove origin
    gh repo create zfs-raidz1-rescue --public --source=. --remote=origin --push

Pour une mise a jour ulterieure, apres un nouveau paquet :

    cd ~/zfs-raidz1-rescue && git pull $PAQUET main && git push
TXT
