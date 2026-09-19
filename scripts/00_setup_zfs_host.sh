#!/usr/bin/env bash
# =============================================================================
# 00_setup_zfs_host.sh
# Installe OpenZFS (userland + module noyau via DKMS) sur la machine de DEV.
#
# ATTENTION : ce script modifie la machine de developpement (paquets).
#             Il ne touche AUCUN disque, AUCUNE image, AUCUNE donnee ZFS.
#
# A executer en root :   sudo bash scripts/00_setup_zfs_host.sh
# =============================================================================
set -euo pipefail

KVER="$(uname -r)"
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ce script doit etre lance en root (sudo)."

log "Noyau en cours d'execution : $KVER"

# --- 1. Activer le composant 'contrib' (zfs-dkms y reside) --------------------
log "Activation du composant 'contrib' dans /etc/apt/sources.list"
if grep -qE '^deb .*trixie.*\bcontrib\b' /etc/apt/sources.list 2>/dev/null; then
  echo "    contrib deja actif."
else
  cp -a /etc/apt/sources.list "/etc/apt/sources.list.bak.$(date +%Y%m%d%H%M%S)"
  sed -i -E 's#^(deb(-src)? +http[^ ]+ +trixie[^ ]* +main)([[:space:]]|$)#\1 contrib\3#' /etc/apt/sources.list
  grep -E '^deb ' /etc/apt/sources.list | sed 's/^/    /'
fi

# --- 2. Index + paquets -------------------------------------------------------
log "apt update"
apt-get update -qq

HDR="linux-headers-${KVER}"
PKGS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pkgs"
export DEBIAN_FRONTEND=noninteractive

log "En-tetes du noyau : $HDR"
if dpkg -s "$HDR" >/dev/null 2>&1; then
  echo "    deja installes."
elif apt-cache show "$HDR" >/dev/null 2>&1; then
  echo "    presents dans l'index apt."
  apt-get install -y --no-install-recommends "$HDR"
else
  # Le noyau qui tourne (6.12.90+deb13.1) a ete retire de l'index apt par une
  # publication plus recente. Les .deb EXACTS ont ete recuperes depuis
  # snapshot.debian.org (archive officielle Debian) dans ./pkgs.
  echo "    absents de l'index apt -> installation depuis $PKGS (snapshot.debian.org)"
  ls "$PKGS"/linux-*"${KVER%-amd64}"*.deb >/dev/null 2>&1 \
    || die "aucun .deb d'en-tetes pour $KVER dans $PKGS"
  apt-get install -y --no-install-recommends "$PKGS"/linux-headers-*.deb "$PKGS"/linux-kbuild-*.deb
fi
[[ -d "/lib/modules/$KVER/build" ]] || die "/lib/modules/$KVER/build absent apres installation des en-tetes"

log "Installation : build-essential, dkms, zfs-dkms, zfsutils-linux"
apt-get install -y --no-install-recommends \
    build-essential dkms zfs-dkms zfsutils-linux

# --- 3. Chargement du module --------------------------------------------------
log "Chargement du module zfs"
modprobe zfs || die "modprobe zfs a echoue (voir: dkms status ; dmesg | tail -40)"

log "Verification"
zfs version
ls -l /dev/zfs
echo
printf '\033[1;32mOK — OpenZFS operationnel. Etape suivante : sudo bash scripts/10_make_test_pool.sh\033[0m\n'
