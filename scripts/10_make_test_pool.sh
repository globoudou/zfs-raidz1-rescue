#!/usr/bin/env bash
# =============================================================================
# 10_make_test_pool.sh  --  ETAPE 1 du CLAUDE.md
# Cree un environnement de test reproductible :
#   - 4 images disque  : diska.img diskb.img diskc.img diskd.img
#   - 1 pool RAIDZ1 4 vdevs (ashift=12)
#   - des donnees CONNUES (petits/gros fichiers, texte/binaire, arborescences)
#   - une copie de reference hors pool + sha256
#   - des dumps zdb de reference (labels, uberblocks, config, dnodes)
#
# Le pool est ensuite EXPORTE et les images passees en lecture seule (444).
#
# A executer en root :   sudo bash scripts/10_make_test_pool.sh
# =============================================================================
set -euo pipefail

PROJ="${PROJ:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LAB="${LAB:-$PROJ/testlab}"
IMG="$LAB/images"
REF="$LAB/reference"
META="$LAB/zdb"
POOL="${POOL:-zrtest}"
MNT="/mnt/$POOL"
IMG_SIZE="${IMG_SIZE:-512M}"
ASHIFT="${ASHIFT:-12}"
# proprietaire des fichiers produits : l'utilisateur qui a lance sudo
OWNER="${OWNER:-${SUDO_USER:-$(id -un)}}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ce script doit etre lance en root (sudo)."
command -v zpool >/dev/null || die "zpool absent : lancer d'abord scripts/00_setup_zfs_host.sh"
zpool list "$POOL" >/dev/null 2>&1 && die "Le pool '$POOL' est deja importe. Faire: zpool export $POOL"

# --- 0. Nettoyage d'un run precedent -----------------------------------------
if [[ -d "$IMG" ]]; then
  log "Suppression de l'environnement de test precedent ($LAB)"
  chmod -R u+w "$LAB" 2>/dev/null || true
  rm -rf "$LAB"
fi
mkdir -p "$IMG" "$REF" "$META"

# --- 1. Images disque ---------------------------------------------------------
log "Creation des 4 images de $IMG_SIZE (sparse) : disk{a,b,c,d}.img"
for d in a b c d; do
  truncate -s "$IMG_SIZE" "$IMG/disk$d.img"
done
ls -l "$IMG"

# --- 2. Jeu de donnees de reference (genere AVANT, conserve hors pool) --------
log "Generation du jeu de donnees de reference dans $REF"
python3 - "$REF" <<'PYEOF'
import os, sys, random, hashlib
ref = sys.argv[1]

def w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)

# --- petits fichiers (doivent tenir dans 1 bloc -> fortes chances de survie) ---
rnd = random.Random(1234)
for i in range(200):
    size = rnd.choice([64, 137, 512, 1024, 2048, 3500])
    data = bytes(rnd.getrandbits(8) for _ in range(size))
    w(f"{ref}/small/file_{i:03d}.bin", data)

# --- texte compressible, arborescence ----------------------------------------
lorem = (b"Le systeme de fichiers ZFS utilise des blocs de taille variable et "
         b"des block pointers contenant jusqu a trois DVA. ")
for year in (2023, 2024, 2025):
    for m in range(1, 4):
        for k in range(5):
            n = 50 * (k + 1) + year % 7
            w(f"{ref}/docs/{year}/{m:02d}/note_{k}.txt", lorem * n)
w(f"{ref}/docs/gros_texte_2M.txt", lorem * 21000)          # ~2 Mo compressible
w(f"{ref}/docs/moyen_200K.txt",  lorem * 2100)             # ~200 Ko

# --- binaires incompressibles -------------------------------------------------
for name, size in (("blob_64K.bin", 64*1024),
                   ("blob_1M.bin", 1024*1024),
                   ("blob_7M.bin", 7*1024*1024)):
    w(f"{ref}/blobs/{name}", os.urandom(size))

# --- gros fichiers multi-stripes ---------------------------------------------
w(f"{ref}/big/big_32M.bin", os.urandom(32*1024*1024))
w(f"{ref}/big/big_64M.bin", os.urandom(64*1024*1024))
# gros fichier compressible (motifs) pour tester la decompression
w(f"{ref}/big/big_48M_compressible.bin", (b"ABCD1234" * 128) * 6 * 1024)

# --- cas particuliers ---------------------------------------------------------
w(f"{ref}/edge/vide.bin", b"")
w(f"{ref}/edge/un_octet.bin", b"Z")
w(f"{ref}/edge/exact_128K.bin", os.urandom(128*1024))
w(f"{ref}/edge/exact_128K_plus_1.bin", os.urandom(128*1024 + 1))
print("jeu de donnees genere")
PYEOF

log "Calcul des sha256 de reference"
( cd "$REF" && find . -type f | sort | xargs -d '\n' sha256sum > "$LAB/reference_sha256.txt" )
wc -l "$LAB/reference_sha256.txt"

# --- 3. Creation du pool RAIDZ1 ----------------------------------------------
log "Creation du pool RAIDZ1 '$POOL' (ashift=$ASHIFT) sur les 4 images"
zpool create -f \
  -o ashift="$ASHIFT" \
  -o autoexpand=off \
  -O atime=off \
  -O xattr=sa \
  -O compression=off \
  -O dedup=off \
  -O mountpoint="$MNT" \
  "$POOL" raidz1 "$IMG/diska.img" "$IMG/diskb.img" "$IMG/diskc.img" "$IMG/diskd.img"

log "Creation des datasets"
zfs create -o compression=off -o recordsize=128K "$POOL/small"
zfs create -o compression=lz4 -o recordsize=128K "$POOL/docs"
zfs create -o compression=off -o recordsize=128K "$POOL/blobs"
zfs create -o compression=lz4 -o recordsize=1M   "$POOL/big"
zfs create -o compression=off -o recordsize=128K "$POOL/edge"

log "Copie des donnees dans le pool"
for ds in small docs blobs big edge; do
  cp -a "$REF/$ds/." "$MNT/$ds/"
done
sync; zpool sync "$POOL"

log "Un snapshot (utile pour l'etape 'snapshots' du CLAUDE.md)"
zfs snapshot -r "$POOL@base"

log "Etat du pool"
zpool status -v "$POOL"
zpool list -v "$POOL"
zfs list -o name,used,referenced,compressratio,recordsize,compression

# --- 4. Verite terrain : inode (= numero d'objet dnode) de chaque fichier -----
log "Capture de la table fichier -> objet dnode (verite terrain)"
( cd "$MNT" && find . -mindepth 1 -printf '%y %i %s %p\n' | sort -k4 ) > "$LAB/ground_truth_objects.txt"
head -5 "$LAB/ground_truth_objects.txt"; wc -l "$LAB/ground_truth_objects.txt"

log "Verification des sha256 depuis le pool monte (doit etre 100% OK)"
( cd "$MNT" && sha256sum -c --quiet "$LAB/reference_sha256.txt" 2>&1 | head -20 ) \
  && echo "    tous les fichiers du pool sont identiques a la reference"

# --- 5. Dumps zdb de reference (pool encore importe) --------------------------
log "Dumps zdb (reference pour valider notre parseur)"
zdb -C "$POOL"            > "$META/zdb_C_config.txt"        2>&1 || true
zdb -u "$POOL"            > "$META/zdb_u_uberblock.txt"     2>&1 || true
zdb -d "$POOL"            > "$META/zdb_d_datasets.txt"      2>&1 || true
zdb -dddd "$POOL/small"   > "$META/zdb_dddd_small.txt"      2>&1 || true
zdb -dddd "$POOL/edge"    > "$META/zdb_dddd_edge.txt"       2>&1 || true
zdb -dddd "$POOL/big"     > "$META/zdb_dddd_big.txt"        2>&1 || true
zdb -mm   "$POOL"         > "$META/zdb_mm_metaslabs.txt"    2>&1 || true
zpool get all "$POOL"     > "$META/zpool_get_all.txt"       2>&1 || true
zfs  get all -r "$POOL"   > "$META/zfs_get_all.txt"         2>&1 || true

# --- 6. Export + gel des images ----------------------------------------------
log "Export du pool (aucune ecriture ulterieure)"
zpool export "$POOL"

log "Dump des 4 labels de chaque image (zdb -l)"
for d in a b c d; do
  zdb -l "$IMG/disk$d.img" > "$META/zdb_l_disk$d.txt" 2>&1 || true
done
grep -Hn -E '^ +(guid|ashift|txg|id|path|type):' "$META"/zdb_l_disk?.txt | head -40 || true

log "Passage des images en LECTURE SEULE + empreintes"
chmod 444 "$IMG"/disk?.img
sha256sum "$IMG"/disk?.img > "$LAB/images_sha256.txt"
cat "$LAB/images_sha256.txt"

chown -R "$OWNER":"$OWNER" "$LAB" 2>/dev/null || true
chmod 444 "$IMG"/disk?.img

log "Termine"
cat <<TXT

  Images        : $IMG/disk{a,b,c,d}.img   (mode 444, pool EXPORTE)
  Reference     : $REF  + $LAB/reference_sha256.txt
  Verite terrain: $LAB/ground_truth_objects.txt
  Dumps zdb     : $META
  Empreintes    : $LAB/images_sha256.txt

  Scenario 2 disques manquants :  bash scripts/20_scenario_missing.sh a b
TXT
