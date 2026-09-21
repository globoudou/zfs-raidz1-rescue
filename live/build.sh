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
rm -rf "$WORK/iso" "$WORK/payload"
mkdir -p "$WORK/iso/live" "$WORK/iso/boot/grub" "$WORK/payload"
cp "$LIVE/grub/grub.cfg" "$WORK/iso/boot/grub/grub.cfg"

# --- 3. contenu du projet a embarquer ----------------------------------------
log "Extraction du projet (contenu suivi par git, donc deja anonymise)"
if git -C "$PROJ" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$PROJ" archive --format=tar HEAD | tar -x -C "$WORK/payload"
    info "revision $(git -C "$PROJ" rev-parse --short HEAD)"
else
    die "ce repertoire n'est pas un depot git : impossible de garantir le contenu"
fi
info "$(find "$WORK/payload" -type f | wc -l) fichiers embarques"

# --- 4. liste des paquets -----------------------------------------------------
PAQUETS=$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/[[:space:]]//g' \
              "$LIVE/packages.list" | paste -sd, -)
info "$(tr ',' '\n' <<< "$PAQUETS" | wc -l) paquets demandes"

# --- 5. construction du systeme de fichiers ----------------------------------
log "Construction du systeme (telechargement + installation, plusieurs minutes)"
mmdebstrap \
    --mode=unshare \
    --architectures="$ARCH" \
    --variant=important \
    --components='main contrib' \
    --aptopt='Apt::Install-Recommends "false"' \
    --include="$PAQUETS" \
    --customize-hook="tar -C \"$LIVE/overlay\" -cf - . | tar -C \"\$1\" -xf -" \
    --customize-hook="mkdir -p \"\$1/opt/zfs-raidz1-rescue\" && \
        tar -C \"$WORK/payload\" -cf - . | \
        tar -C \"\$1/opt/zfs-raidz1-rescue\" -xf -" \
    --customize-hook="cp \"$LIVE/hooks/configure.sh\" \"\$1/tmp/configure.sh\" && \
        chroot \"\$1\" /tmp/configure.sh && rm -f \"\$1/tmp/configure.sh\"" \
    --customize-hook="rm -rf \"$WORK/boot\" && cp -a \"\$1/boot\" \"$WORK/boot\"" \
    --customize-hook="mksquashfs \"\$1\" '$WORK/iso/live/filesystem.squashfs' \
        -comp xz -Xbcj x86 -b 1M -noappend -no-progress \
        -e boot -e proc -e sys -e dev/pts" \
    "$SUITE" /dev/null "deb $MIRROR $SUITE main contrib"

[[ -f "$WORK/iso/live/filesystem.squashfs" ]] || die "squashfs non produit"
info "squashfs : $(du -h "$WORK/iso/live/filesystem.squashfs" | cut -f1)"

# --- 6. noyau et initrd -------------------------------------------------------
log "Noyau et initrd"
shopt -s nullglob
noyaux=("$WORK/boot"/vmlinuz-*)
initrds=("$WORK/boot"/initrd.img-*)
((${#noyaux[@]})) || die "aucun noyau trouve dans l'image"
((${#initrds[@]})) || die "aucun initrd trouve dans l'image"
cp "${noyaux[-1]}" "$WORK/iso/live/vmlinuz"
cp "${initrds[-1]}" "$WORK/iso/live/initrd.img"
info "$(basename "${noyaux[-1]}")  +  $(basename "${initrds[-1]}")"

# --- 7. image ISO hybride (BIOS + UEFI) --------------------------------------
log "Assemblage de l'image ISO"
mkdir -p "$(dirname "$ISO")"
grub-mkrescue --output="$ISO" --volid="$VOLID" "$WORK/iso" \
    -- -volid "$VOLID" 2>&1 | grep -vE '^xorriso : NOTE|^Drive current' || true
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
