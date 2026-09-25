# Étape 10 — Procédure pour les vrais disques

Rien de ce qui suit ne doit être fait dans le désordre. Les deux disques
survivants sont la seule source de données : **ils ne doivent jamais être
montés, importés ni écrits**.

## 1. Avant de brancher quoi que ce soit

- Ne **jamais** exécuter `zpool import` (même en lecture seule) sur les vrais
  disques tant que les images ne sont pas faites : un import écrit les labels
  et peut déclencher un resilver.
- Si les disques ont été branchés sur une machine où le service ZFS tourne,
  l'import peut être automatique. Sur cette machine de développement, désactiver
  l'import automatique avant de brancher :
  ```bash
  sudo systemctl mask zfs-import-cache.service zfs-import-scan.service
  sudo systemctl mask zfs-mount.service zfs.target
  ```
- Brancher les disques **en lecture seule** si un bloqueur d'écriture matériel
  est disponible. Sinon, au minimum :
  ```bash
  sudo blockdev --setro /dev/sdX
  ```

## 2. Faire les images bit-à-bit

`ddrescue` est préférable à `dd` : il gère les secteurs illisibles, tient un
journal et permet de reprendre.

```bash
sudo apt install gddrescue

# premier passage : rapide, sans réessais
sudo ddrescue -f -n /dev/sdX /media/travail/disk1.img /media/travail/disk1.map
# second passage : réessais sur les zones difficiles
sudo ddrescue -f -r3 /dev/sdX /media/travail/disk1.img /media/travail/disk1.map
```

Puis, pour chaque image :

```bash
sha256sum /media/travail/disk1.img | tee /media/travail/disk1.img.sha256
chmod 444 /media/travail/disk1.img
```

Conserver `disk1.map` : il dit **exactement** quels secteurs n'ont pas pu être
lus. Un secteur illisible n'est pas un secteur vide — l'outil doit le savoir.

> Si `ddrescue` signale des zones non lues, indiquez-le moi : l'outil les
> traitera comme des colonnes absentes plutôt que comme des données valides.

### Disques partitionnés (FreeBSD / FreeNAS / TrueNAS)

Relevez la table de partitions **avant** d'imager :

```bash
sudo sfdisk -d /dev/sdX | tee /media/travail/disk1.parttable.txt
lsblk -o NAME,SIZE,TYPE,PARTTYPENAME /dev/sdX
```

Si vous voyez une disposition de ce genre, le pool est dans la partition 2 :

```
/dev/sdc1   2G    FreeBSD swap
/dev/sdc2   1,8T  FreeBSD ZFS
```

**Imagez quand même le disque entier** (`/dev/sdX`, pas `/dev/sdX2`) : c'est la
bonne pratique forensic, et l'outil retrouve seul la partition ZFS à
l'intérieur de l'image. Pour analyser directement les disques sans image, vous
pouvez pointer soit le disque (`/dev/sda`), soit la partition (`/dev/sda2`) :
le résultat est le même.

## 3. Mettre les originaux hors ligne

Débrancher les disques physiques et les ranger. Toute la suite se fait sur les
images, sur un troisième support.

## 4. Informations à relever (étape 0 du cahier des charges)

| Élément | Où le trouver |
|---|---|
| taille exacte de chaque disque | `blockdev --getsize64 /dev/sdX` |
| taille de secteur logique / physique | `blockdev --getss /dev/sdX`, `--getpbsz` |
| système qui hébergeait le pool | vous ; le champ `hostname` des labels le confirmera |
| version de ZFS | le rapport de l'étape 2 (`version`, `features_for_read`) |
| position des disques survivants dans le vdev | le rapport de l'étape 2 (`col[N]`) |

La **position** (`col[N]`) est déterminante : voir `docs/etape3_mapping_raidz.md`.
Deux colonnes voisines alignées (`{0,1}` ou `{2,3}`) donnent un résultat très
inférieur à deux colonnes décalées.

## 5. Analyse, sans extraction

```bash
bash scripts/70_analyse_reelle.sh /media/travail/disk1.img /media/travail/disk2.img
```

Produit un répertoire de rapports (labels, uberblocks, MOS, fichiers,
simulation d'extraction) et **vérifie les empreintes SHA-256 avant et après**.

À lire dans l'ordre :

1. `02_labels.txt` — le pool est-il bien celui attendu ? topologie, colonnes
   MISSING, cohérences ;
2. `04_uberblocks.txt` — quel txg utiliser ; présence d'un checkpoint, de MMP,
   d'une expansion RAIDZ (qui serait hors périmètre) ;
3. `05_mos.txt` — datasets et snapshots retrouvés ;
4. `06_fichiers.txt` — arborescence récupérable ; les entrées marquées `~` ont
   des métadonnées reconstituées par hypothèse ;
5. `07_extraction_simulee.txt` — ce qui sortirait réellement, fichier par
   fichier, avec l'état de chaque bloc.

## 6. Extraction

Seulement après lecture du rapport, et vers un **troisième support** :

```bash
python3 -m zfsrescue extract /media/travail/disk1.img /media/travail/disk2.img \
        --dest /media/destination/recuperation --blocks \
        --json /media/destination/recuperation_rapport.json
```

Options utiles :

- `--dataset zrtest/photos` : traiter un dataset à la fois (recommandé sur un
  gros pool) ;
- `--skip-incomplete` : n'écrire que les fichiers intégralement récupérés ;
- `--no-fill` : tronquer au premier trou plutôt que combler avec des zéros ;
- `--txg N` : repartir d'un état antérieur du pool si le plus récent est moins
  exploitable.

Puis re-vérifier les images :

```bash
sha256sum -c /media/travail/disk1.img.sha256
```

## 7. Ce qu'il faut me transmettre pour que je puisse aider

- `02_labels.json` et `04_uberblocks.json` (ils ne contiennent aucune donnée de
  fichier, uniquement des métadonnées de pool) ;
- le résumé de `07_extraction_simulee.txt` ;
- les éventuels messages d'erreur.

## Points de vigilance propres aux vraies données

| Situation | Comportement de l'outil |
|---|---|
| compression **zstd** | supportée seulement si `python3-zstandard` est installé, sinon l'outil le dit et n'invente rien (`sudo apt install python3-zstandard`) |
| checksum **skein / edonr / blake3 / sha512** | non implémentés : les blocs concernés sont refusés plutôt que acceptés sans vérification |
| pool **chiffré** | détecté et refusé (hors périmètre) |
| **expansion RAIDZ** passée | détectée dans l'uberblock et signalée : le mapping standard ne s'applique plus, il faudra étendre l'outil |
| blocs **gang** | détectés, marqués `UNKNOWN`, jamais devinés |
| `ashift=9` | géré par le mapping (couvert par les tests d'invariants) mais jamais confronté à un vrai pool : à vérifier sur vos images |
| secteurs illisibles dans l'image | traités comme une colonne absente si la lecture échoue |
| **volume** | fletcher-4 tourne en Python pur (~5 Mo/s) : sur un pool de plusieurs téraoctets, extraire dataset par dataset, ou accélérer le calcul (numpy / extension C) avant de lancer le tout |

## Rappel de la règle fondamentale

L'outil ne déclare récupérée aucune donnée dont le checksum n'a pas été
vérifié, et ne comble jamais une colonne absente par des zéros dans ses
calculs. Tout ce qui est incertain est marqué comme tel dans les rapports.
