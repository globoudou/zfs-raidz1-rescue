#!/usr/bin/env bash
# =============================================================================
# 90_verify_readonly.sh -- controle forensic : les images n'ont PAS ete modifiees
# A relancer apres chaque execution de l'outil de recuperation.
# =============================================================================
set -euo pipefail
PROJ="${PROJ:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LAB="${LAB:-$PROJ/testlab}"
cd "$LAB"
echo "Verification des empreintes des images source..."
if sha256sum -c images_sha256.txt; then
  echo -e "\033[1;32mOK : aucune image source n'a ete modifiee.\033[0m"
else
  echo -e "\033[1;31mALERTE : une image source a ete MODIFIEE !\033[0m" >&2
  exit 1
fi
ls -l "$LAB/images"
