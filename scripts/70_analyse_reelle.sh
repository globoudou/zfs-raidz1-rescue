#!/usr/bin/env bash
# =============================================================================
# 70_analyse_reelle.sh — ETAPE 10, phase d'analyse (AUCUNE extraction)
#
#   bash scripts/70_analyse_reelle.sh <image1> <image2> [repertoire_sortie]
#
# Execute toute la chaine en LECTURE SEULE sur de vraies images et produit un
# rapport complet. Verifie les empreintes SHA-256 des images avant et apres :
# elles doivent etre identiques.
#
# N'ecrit RIEN d'autre que dans le repertoire de sortie.
# =============================================================================
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG1="${1:-}"; IMG2="${2:-}"
OUT="${3:-$PROJ/reports/reel_$(date +%Y%m%d_%H%M%S)}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$IMG1" && -n "$IMG2" ]] || die "usage: $0 <image1> <image2> [sortie]"
for f in "$IMG1" "$IMG2"; do
  [[ -e "$f" ]] || die "$f introuvable"
  [[ -r "$f" ]] || die "$f illisible"
done
mkdir -p "$OUT"

log "Contexte"
{
  echo "date          : $(date -Is)"
  echo "machine       : $(uname -a)"
  echo "zfsrescue     : $(cd "$PROJ" && python3 -m zfsrescue --version 2>&1)"
  echo "zdb           : $( (zdb -V 2>/dev/null || /usr/sbin/zdb -V 2>/dev/null) | head -1)"
  echo "image 1       : $IMG1"
  echo "image 2       : $IMG2"
  ls -l "$IMG1" "$IMG2"
} | tee "$OUT/00_contexte.txt"

log "Empreintes AVANT analyse (peut etre long)"
sha256sum "$IMG1" "$IMG2" | tee "$OUT/01_sha256_avant.txt"

cd "$PROJ"
log "Etape 2 — labels et topologie"
python3 -m zfsrescue labels "$IMG1" "$IMG2" --full-labels \
    --json "$OUT/02_labels.json" | tee "$OUT/02_labels.txt" || true

log "Etape 4 — uberblocks"
python3 -m zfsrescue uberblocks "$IMG1" "$IMG2" --limit 40 \
    --json "$OUT/04_uberblocks.json" | tee "$OUT/04_uberblocks.txt" || true

log "Etape 5 — MOS et datasets"
python3 -m zfsrescue mos "$IMG1" "$IMG2" --objects \
    --json "$OUT/05_mos.json" | tee "$OUT/05_mos.txt" || true

log "Etape 6 — fichiers recuperables"
python3 -m zfsrescue ls "$IMG1" "$IMG2" --files --limit 200 \
    --json "$OUT/06_fichiers.json" | tee "$OUT/06_fichiers.txt" || true

log "Etapes 7-8 — simulation d'extraction (aucune ecriture de donnees)"
python3 -m zfsrescue extract "$IMG1" "$IMG2" --dry-run --blocks \
    --json "$OUT/07_extraction_simulee.json" \
    | tee "$OUT/07_extraction_simulee.txt" || true

log "Empreintes APRES analyse"
sha256sum "$IMG1" "$IMG2" | tee "$OUT/08_sha256_apres.txt"

if diff -q "$OUT/01_sha256_avant.txt" "$OUT/08_sha256_apres.txt" >/dev/null; then
  echo -e "\n\033[1;32mOK : les images sont bit-a-bit identiques avant et apres.\033[0m"
else
  echo -e "\n\033[1;31mALERTE : une image a change pendant l'analyse !\033[0m" >&2
  diff "$OUT/01_sha256_avant.txt" "$OUT/08_sha256_apres.txt" >&2 || true
  exit 1
fi

cat <<TXT

Rapport complet : $OUT
  00_contexte.txt            environnement d'execution
  01/08_sha256_*.txt         preuve de non-modification
  02_labels.*                pool, topologie, colonnes MISSING
  04_uberblocks.*            etats du pool exploitables (txg)
  05_mos.*                   objets du MOS, datasets, snapshots
  06_fichiers.*              arborescence recuperable
  07_extraction_simulee.*    ce qui sortirait, fichier par fichier

Prochaine etape, seulement apres lecture du rapport :
  python3 -m zfsrescue extract "$IMG1" "$IMG2" --dest /un/troisieme/support --blocks \\
      --json "$OUT/09_extraction.json"
TXT
