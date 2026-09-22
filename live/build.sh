#!/usr/bin/env bash
# =============================================================================
# build.sh — construit une image ISO/USB amorcable du systeme de recuperation.
#
#   bash live/build.sh [chemin/de/sortie.iso]
#
# Particularite : la construction se fait SANS DROITS ROOT, grace au mode
# « unshare » de mmdebstrap (espaces de noms utilisateur). Seule l'installation
# des outils de construction demande sudo, une fois :
#
#   sudo apt install -y mmdebstrap squashfs-tools xorriso \
#        grub-efi-amd64-bin grub-pc-bin mtools dosfstools
#
# L'image produite :
#   * amorce en BIOS et en UEFI ;
#   * passe tous les disques en LECTURE SEULE au demarrage ;
#   * n'importe jamais de pool ZFS et interdit le chargement du module noyau ;
#   * embarque l'outil, sa documentation, ddrescue, smartmontools et zdb.
# =============================================================================
set -euo pipefail

LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$LIVE/.." && pwd)"

# Les hooks de mmdebstrap s'executent dans un espace de noms ou l'UID 0 interne
# correspond a une plage « subuid » (voir /etc/subuid), et NON a l'utilisateur
# qui lance la construction. Cet UID n'a donc aucun droit sur un repertoire
# personnel en mode 700 : toutes les entrees et sorties de la construction
# doivent transiter par un repertoire accessible a tous, hors du home.
STAGE_BASE="${STAGE_BASE:-/var/tmp}"

SUITE="${SUITE:-trixie}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
ARCH="${ARCH:-amd64}"
WORK="${WORK:-$PROJ/build}"
ISO="${1:-$WORK/zfs-raidz1-rescue-$(date +%Y%m%d).iso}"
VOLID="${VOLID:-ZFSRESCUE}"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. prerequis -------------------------------------------------------------
log "Verification des prerequis"
manquants=()
for c in mmdebstrap mksquashfs xorriso grub-mkrescue; do
    command -v "$c" >/dev/null || manquants+=("$c")
done
if ((${#manquants[@]})); then
    die "outils manquants : ${manquants[*]}
  sudo apt install -y mmdebstrap squashfs-tools xorriso \\
       grub-efi-amd64-bin grub-pc-bin mtools dosfstools"
fi
unshare --user --map-root-user true 2>/dev/null \
    || die "les espaces de noms utilisateur ne sont pas disponibles :
  la construction non privilegiee est impossible sur cette machine.
  Relancer ce script avec sudo, ou activer :
      sudo sysctl -w kernel.unprivileged_userns_clone=1"
info "mmdebstrap  $(mmdebstrap --version 2>&1 | head -1)"
info "suite       $SUITE ($ARCH), miroir $MIRROR"

libre=$(df -Pk "$(dirname "$WORK")" | awk 'NR==2{print int($4/1024)}')
info "espace libre : ${libre} Mio"
(( libre > 4000 )) || die "il faut au moins 4 Gio libres pour construire l'image"

# --- 2. arborescence de travail ----------------------------------------------
log "Preparation de $WORK"
rm -rf "$WORK/iso"
mkdir -p "$WORK/iso/live" "$WORK/iso/boot/grub"
cp "$LIVE/grub/grub.cfg" "$WORK/iso/boot/grub/grub.cfg"

STAGE="$(mktemp -d "$STAGE_BASE/zfsrescue-build.XXXXXX")"
# conserve le repertoire d'echange en cas d'echec, pour diagnostic
trap 'code=$?; if ((code)); then
        printf "\n    (repertoire de travail conserve : %s)\n" "$STAGE"
      else
        # les fichiers produits par les hooks appartiennent a un subuid :
        # seul un espace de noms qui mappe cette plage peut les supprimer
        rm -rf "$STAGE" 2>/dev/null \
          || unshare --user --map-auto --map-root-user rm -rf "$STAGE" 2>/dev/null \
          || printf "    (a supprimer manuellement : %s)\n" "$STAGE"
      fi' EXIT
mkdir -p "$STAGE/in" "$STAGE/out"
chmod 755 "$STAGE" "$STAGE/in"
chmod 777 "$STAGE/out"          # ecrit par l'UID interne de l'espace de noms
info "repertoire d'echange : $STAGE"

cp -a "$LIVE/overlay" "$LIVE/hooks" "$STAGE/in/"
chmod -R a+rX "$STAGE/in"

# --- 3. contenu du projet a embarquer ----------------------------------------
log "Extraction du projet (contenu suivi par git, donc deja anonymise)"
if git -C "$PROJ" rev-parse --git-dir >/dev/null 2>&1; then
    mkdir -p "$STAGE/in/payload"
    git -C "$PROJ" archive --format=tar HEAD | tar -x -C "$STAGE/in/payload"
    info "revision $(git -C "$PROJ" rev-parse --short HEAD)"
else
    die "ce repertoire n'est pas un depot git : impossible de garantir le contenu"
fi
chmod -R a+rX "$STAGE/in/payload"
info "$(find "$STAGE/in/payload" -type f | wc -l) fichiers embarques"

# --- 4. liste des paquets -----------------------------------------------------
PAQUETS=$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/[[:space:]]//g' \
              "$LIVE/packages.list" | paste -sd, -)
info "$(tr ',' '\n' <<< "$PAQUETS" | wc -l) paquets demandes"

# --- 5. construction du systeme de fichiers ----------------------------------
log "Construction du systeme (telechargement + installation, plusieurs minutes)"
export TMPDIR="$STAGE_BASE"          # le chroot temporaire depasse la taille du tmpfs
mmdebstrap \
    --mode=unshare \
    --architectures="$ARCH" \
    --variant=important \
    --components='main contrib' \
    --aptopt='Apt::Install-Recommends "false"' \
    --include="$PAQUETS" \
    --customize-hook="tar -C \"$STAGE/in/overlay\" -cf - . | tar -C \"\$1\" -xf -" \
    --customize-hook="mkdir -p \"\$1/opt/zfs-raidz1-rescue\" && \
        tar -C \"$STAGE/in/payload\" -cf - . | \
        tar -C \"\$1/opt/zfs-raidz1-rescue\" -xf -" \
    --customize-hook="cp \"$STAGE/in/hooks/configure.sh\" \"\$1/tmp/configure.sh\" && \
        chroot \"\$1\" /tmp/configure.sh && rm -f \"\$1/tmp/configure.sh\"" \
    --customize-hook="cp \"\$1\"/boot/vmlinuz-* \"\$1\"/boot/initrd.img-* \"$STAGE/out/\"" \
    --customize-hook="mksquashfs \"\$1\" '$STAGE/out/filesystem.squashfs' \
        -comp xz -Xbcj x86 -b 1M -noappend -no-progress \
        -e boot -e proc -e sys -e dev/pts" \
    "$SUITE" /dev/null "deb $MIRROR $SUITE main contrib"

log "Recuperation des produits de la construction"
[[ -f "$STAGE/out/filesystem.squashfs" ]] || die "squashfs non produit"
cp "$STAGE/out/filesystem.squashfs" "$WORK/iso/live/filesystem.squashfs"

info "squashfs : $(du -h "$WORK/iso/live/filesystem.squashfs" | cut -f1)"

# --- 6. noyau et initrd -------------------------------------------------------
log "Noyau et initrd"
shopt -s nullglob
noyaux=("$STAGE/out"/vmlinuz-*)
initrds=("$STAGE/out"/initrd.img-*)
((${#noyaux[@]})) || die "aucun noyau trouve dans l'image"
((${#initrds[@]})) || die "aucun initrd trouve dans l'image"
cp "${noyaux[-1]}" "$WORK/iso/live/vmlinuz"
cp "${initrds[-1]}" "$WORK/iso/live/initrd.img"
info "$(basename "${noyaux[-1]}")  +  $(basename "${initrds[-1]}")"

# --- 7. image ISO hybride (BIOS + UEFI) --------------------------------------
log "Assemblage de l'image ISO"
mkdir -p "$(dirname "$ISO")"
# Note : grub-mkrescue transmet a xorriso tout ce qui suit « -- ». Le nom de
# volume se passe donc par -volid (option de l'emulation mkisofs), jamais par
# --volid, que xorriso refuse.
grub-mkrescue -o "$ISO" "$WORK/iso" -- -volid "$VOLID" 2>&1 \
    | grep -vE '^xorriso : (NOTE|UPDATE)|^Drive current|^Media ' || true
[[ -f "$ISO" ]] || die "l'image n'a pas ete produite"

sha256sum "$ISO" > "$ISO.sha256"

log "Termine"
cat <<TXT

  Image      : $ISO
  Taille     : $(du -h "$ISO" | cut -f1)
  Empreinte  : $(cut -c1-32 < "$ISO.sha256")...

  Ecriture sur une cle USB (DETRUIT son contenu) :

      lsblk -o NAME,SIZE,MODEL,TRAN          # reperer la cle
      sudo dd if=$ISO of=/dev/sdX bs=4M status=progress oflag=direct conv=fsync

  L'image est hybride : elle amorce en BIOS comme en UEFI.
  Au demarrage, tous les disques sont passes en lecture seule.

TXT
