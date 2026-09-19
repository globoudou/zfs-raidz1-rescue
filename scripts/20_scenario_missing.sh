#!/usr/bin/env bash
# =============================================================================
# 20_scenario_missing.sh  --  prepare un scenario "N disques detruits"
#
#   bash scripts/20_scenario_missing.sh a b      # diska + diskb = MISSING
#
# Ne copie ni ne modifie aucune image : cree un repertoire de scenario ne
# contenant que des liens symboliques vers les images SURVIVANTES.
# =============================================================================
set -euo pipefail

PROJ="${PROJ:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LAB="${LAB:-$PROJ/testlab}"
IMG="$LAB/images"
ALL=(a b c d)

[[ $# -ge 1 ]] || { echo "usage: $0 <lettre_manquante> [lettre_manquante...]" >&2; exit 1; }
[[ -d "$IMG" ]] || { echo "ERREUR: $IMG introuvable (lancer 10_make_test_pool.sh)" >&2; exit 1; }

MISSING=("$@")
NAME="missing_$(IFS=_; echo "${MISSING[*]}")"
SCEN="$LAB/scenarios/$NAME"
rm -rf "$SCEN"; mkdir -p "$SCEN"

is_missing() { local x=$1; for m in "${MISSING[@]}"; do [[ $m == "$x" ]] && return 0; done; return 1; }

surv=(); miss=()
for d in "${ALL[@]}"; do
  if is_missing "$d"; then
    miss+=("disk$d.img")
  else
    ln -s "../../images/disk$d.img" "$SCEN/disk$d.img"
    surv+=("disk$d.img")
  fi
done

{
  echo "{"
  echo "  \"scenario\": \"$NAME\","
  echo "  \"vdev_count\": ${#ALL[@]},"
  echo "  \"raid\": \"raidz1\","
  printf '  "missing": ['; printf '"%s"' "${miss[0]}"; for m in "${miss[@]:1}"; do printf ', "%s"' "$m"; done; echo "],"
  printf '  "available": ['; printf '"%s"' "${surv[0]}"; for s in "${surv[@]:1}"; do printf ', "%s"' "$s"; done; echo "]"
  echo "}"
} > "$SCEN/scenario.json"

echo "Scenario '$NAME' pret : $SCEN"
ls -l "$SCEN"
cat "$SCEN/scenario.json"
