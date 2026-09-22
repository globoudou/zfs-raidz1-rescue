#!/bin/sh
# =============================================================================
# Configuration du systeme live, executee DANS le chroot.
# =============================================================================
set -eu

echo "[live] langue et clavier"
sed -i 's/^# *\(fr_FR.UTF-8 UTF-8\)/\1/; s/^# *\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen >/dev/null
echo 'LANG=fr_FR.UTF-8' > /etc/default/locale

echo "[live] compte root sans mot de passe (systeme live, aucun service reseau)"
passwd -d root >/dev/null

echo "[live] ZFS : aucun import, aucun montage, module noyau interdit"
for u in zfs-import-cache.service zfs-import-scan.service zfs-mount.service \
         zfs-share.service zfs-zed.service zfs-volume-wait.service \
         zfs-load-module.service zfs-import.target zfs-volumes.target zfs.target; do
    systemctl mask "$u" >/dev/null 2>&1 || true
done
cat > /etc/modprobe.d/zfsrescue-no-zfs.conf <<'MOD'
# Le systeme de recuperation n'importe jamais de pool et n'a pas besoin du
# module noyau : zdb fonctionne entierement en espace utilisateur.
# Charger zfs pourrait declencher un import automatique : on l'interdit.
blacklist zfs
install zfs /bin/false
MOD

echo "[live] connexion automatique sur la console"
systemctl enable getty@tty1.service >/dev/null 2>&1 || true

echo "[live] raccourcis"
ln -sfn /opt/zfs-raidz1-rescue /root/zfs-raidz1-rescue
cat >> /root/.bashrc <<'BRC'

cd /opt/zfs-raidz1-rescue 2>/dev/null || true
alias ll='ls -lh'
BRC

echo "[live] verification de l'outil"
env PYTHONPATH=/opt/zfs-raidz1-rescue python3 -m zfsrescue --version

echo "[live] initramfs (live-boot)"
update-initramfs -u -k all >/dev/null 2>&1

echo "[live] nettoyage"
# Ne PAS toucher a /tmp ni au cache apt : mmdebstrap y conserve ses propres
# fichiers de travail et se charge lui-meme de vider les listes et le cache
# apres les hooks. Supprimer /tmp/* casse son nettoyage final.
find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
rm -rf /var/tmp/* 2>/dev/null || true
