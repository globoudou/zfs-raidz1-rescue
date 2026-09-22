# Clé USB amorçable de récupération

Image live Debian 13 contenant l'outil, conçue pour intervenir sur la machine
où se trouvent les disques sans jamais risquer de les modifier.

```bash
sudo apt install -y mmdebstrap squashfs-tools xorriso \
     grub-efi-amd64-bin grub-pc-bin mtools dosfstools   # une seule fois
bash live/build.sh                                      # ~10 min, sans sudo
```

La construction elle-même **ne demande pas les droits root** : `mmdebstrap`
travaille dans un espace de noms utilisateur (`--mode=unshare`). Seule
l'installation des outils ci-dessus exige `sudo`.

## Ce que l'image garantit

| Propriété | Mise en œuvre |
|---|---|
| **Tous les disques en lecture seule au démarrage** | règle udev `99-zfsrescue-readonly.rules` → `blockdev --setro` sur chaque disque détecté (`sd*`, `nvme*`, `hd*`, `mmcblk*`, `vd*`) |
| **Aucun import de pool ZFS** | `zfs-import-cache`, `zfs-import-scan`, `zfs-mount`, `zfs-share`, `zfs-zed`, `zfs.target`… tous masqués |
| **Module noyau ZFS non chargeable** | `blacklist zfs` + `install zfs /bin/false` ; `zfs-dkms` n'est même pas installé |
| **`zdb` disponible quand même** | `zfsutils-linux` seul : `zdb` lit les labels et les pools exportés entièrement en espace utilisateur |
| **Rien n'est monté automatiquement** | pas d'environnement de bureau, pas d'automontage |

Le déverrouillage du support de **destination** est explicite et vérifié :

```
# zfsrescue-unlock /dev/sdc
Disque : /dev/sdc
  *** ATTENTION : des labels ZFS ont ete trouves sur /dev/sdc ***
  Il s'agit tres probablement d'un disque SOURCE.
Taper exactement « OUI ECRIRE SUR sdc » pour autoriser l ecriture :
```

La commande cherche des labels ZFS sur le disque avant de demander
confirmation, et exige une phrase exacte contenant le nom du périphérique.

## Contenu

- **l'outil** dans `/opt/zfs-raidz1-rescue` (commande `zfsrescue` dans le PATH),
  avec sa documentation et ses scripts ;
- **`ddrescue`** pour l'imagerie avec journal des secteurs illisibles ;
- `smartmontools`, `nvme-cli`, `hdparm`, `sdparm`, `lsscsi` pour le diagnostic ;
- `parted`, `gdisk`, `e2fsprogs`, `dosfstools`, `exfatprogs`, `ntfs-3g`,
  `rsync`, `pv` pour préparer et alimenter le support de destination ;
- `python3` + `python3-zstandard` (donc les pools compressés en zstd sont lus) ;
- `tmux`, `htop`, `less`, `nano` ;
- clavier **AZERTY** et locale `fr_FR.UTF-8`, connexion automatique en root.

## Menu de démarrage

| Entrée | Usage |
|---|---|
| **protection en écriture ACTIVE** | le mode normal |
| **charger en mémoire** (`toram`) | le système est copié en RAM : la clé peut être retirée, ce qui libère un port USB (prévoir ~2 Gio de RAM) |
| **démarrage verbeux** | diagnostic si la machine ne démarre pas |
| **SANS protection en écriture** | à n'utiliser qu'en connaissance de cause |

## Écrire l'image sur une clé

```bash
lsblk -o NAME,SIZE,MODEL,TRAN            # repérer la clé — vérifiez deux fois
sudo dd if=build/zfs-raidz1-rescue-AAAAMMJJ.iso of=/dev/sdX \
        bs=4M status=progress oflag=direct conv=fsync
```

L'image est hybride : elle amorce aussi bien en BIOS qu'en UEFI. Le Secure Boot
doit être désactivé (l'image n'est pas signée par Microsoft).

## Déroulement sur site

1. démarrer sur la clé, vérifier l'état des disques :
   ```
   zfsrescue-status
   ```
2. brancher le support de destination, l'autoriser en écriture et le monter :
   ```
   zfsrescue-unlock /dev/sdZ
   mkfs.ext4 /dev/sdZ1        # si nécessaire
   mount /dev/sdZ1 /mnt
   ```
3. imager les deux disques survivants :
   ```
   ddrescue -f -n /dev/sda /mnt/disk1.img /mnt/disk1.map
   ddrescue -f -r3 /dev/sda /mnt/disk1.img /mnt/disk1.map
   sha256sum /mnt/disk1.img | tee /mnt/disk1.img.sha256
   ```
4. analyser **sans extraire** :
   ```
   bash /opt/zfs-raidz1-rescue/scripts/70_analyse_reelle.sh \
        /mnt/disk1.img /mnt/disk2.img /mnt/rapports
   ```
5. extraire, après lecture du rapport :
   ```
   zfsrescue extract /mnt/disk1.img /mnt/disk2.img \
             --dest /mnt/recuperation --blocks --json /mnt/rapport.json
   ```

Le même texte est affiché à chaque connexion (`/etc/motd`).

## Limites connues

- **amd64 uniquement** ; pour une autre architecture, `ARCH=arm64 bash live/build.sh`
  (non testé).
- **Pas de micrologiciels non libres** : si un contrôleur de disque ou une carte
  réseau en réclame, ajouter `firmware-linux-nonfree` (et le composant
  `non-free-firmware`) à `live/packages.list` et reconstruire.
- **Pas de Secure Boot** : à désactiver dans le firmware de la machine.
- **Pas de persistance** : tout ce qui est écrit dans le système live disparaît
  au redémarrage. Les images et les données extraites doivent aller sur le
  support de destination.
- La protection en écriture repose sur le drapeau read-only du noyau. Elle est
  très efficace contre les accidents logiciels, mais ne remplace pas un
  **bloqueur d'écriture matériel** dans un contexte judiciaire.
- Le module noyau ZFS étant interdit, il est impossible d'importer un pool
  depuis cette clé — c'est volontaire.

## Notes de construction

Trois contraintes rencontrées à la mise au point, corrigées dans `live/build.sh`
et signalées ici pour qui voudrait adapter le script :

1. **Les hooks de `mmdebstrap` ne voient pas votre répertoire personnel.**
   En mode `unshare`, l'UID 0 interne correspond à une plage *subuid*
   (`/etc/subuid`), pas à l'utilisateur qui lance la construction : un `$HOME`
   en mode 700 lui est inaccessible. Toutes les entrées et sorties transitent
   donc par un répertoire d'échange dans `/var/tmp`, lisible par tous, dont le
   sous-répertoire de sortie est en mode 777 puisqu'il est écrit par cet UID.
   Les fichiers produits appartiennent à ce subuid : les supprimer ensuite
   demande le même espace de noms (`unshare --user --map-auto`).

2. **Ne rien supprimer dans `/tmp` du chroot.** `mmdebstrap` y conserve sa
   propre configuration apt et s'en sert pour vider les listes de paquets
   *après* les hooks. Un `rm -rf /tmp/*` dans le hook de configuration fait
   échouer tout le nettoyage final.

3. **`grub-mkrescue` n'accepte pas `--volid`.** Le nom de volume se transmet à
   `xorriso` après le séparateur `--`, sous la forme `-volid NOM` (option de
   l'émulation mkisofs).

## Vérifications effectuées sur l'image produite

| Contrôle | Résultat |
|---|---|
| Catalogue El Torito | deux images d'amorçage : BIOS (`i386-pc/eltorito.img`) et UEFI (`efi.img`), avec MBR de protection + GPT — donc écrivable telle quelle sur clé |
| Amorçage live | l'initrd contient `scripts/live` (live-boot), le module `squashfs` et `blockdev` |
| Outil embarqué | `python3 -m zfsrescue --version` exécuté avec succès *dans* le chroot pendant la construction |
| Services ZFS | tous masqués (liens symboliques vers `/dev/null`) |
| Module ZFS | `blacklist zfs` + `install zfs /bin/false`, aucun module `zfs` dans `/lib/modules` |
| `zdb`, `ddrescue`, `python3-zstandard` | présents |
| Clavier / hôte | `XKBLAYOUT="fr"`, nom d'hôte `zfsrescue` |

**Le démarrage réel n'a pas été testé** : aucun émulateur n'est installé sur la
machine de développement. Avant de compter dessus sur site, démarrez la clé une
fois sur une machine de test — ou installez `qemu-system-x86` pour un essai à
blanc.
