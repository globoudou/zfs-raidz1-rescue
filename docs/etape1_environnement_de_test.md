# Étape 1 — Environnement de test reproductible

## Objectif
Disposer d'un pool ZFS RAIDZ1 de 4 vdevs dont **le contenu attendu est connu exactement**,
afin de pouvoir valider l'outil de récupération dans le scénario « 2 disques détruits ».

## Machine de développement (constaté le 2026-09-18)

| Élément | Valeur |
|---|---|
| OS | Debian GNU/Linux 13 (trixie) |
| Noyau | 6.12.90+deb13.1-amd64 |
| ZFS | **absent** (ni userland, ni module, ni `/dev/zfs`) |
| Droits | compte `claude` non privilégié, `sudo` exige un mot de passe |
| Paquet ZFS disponible | `zfs-dkms` **2.3.9-0+deb13u1** (composant `contrib`) |
| En-têtes noyau | `linux-headers-6.12.90+deb13.1-amd64` 6.12.90-2 → **correspondance exacte, pas de reboot nécessaire** |

Créer un pool ZFS exige le module noyau + les droits root : l'installation doit donc
être lancée par l'utilisateur (`scripts/00_setup_zfs_host.sh`).

## Scripts

| Script | Droits | Rôle |
|---|---|---|
| `scripts/00_setup_zfs_host.sh` | root | active `contrib`, installe `linux-headers-$(uname -r)`, `zfs-dkms`, `zfsutils-linux`, charge le module |
| `scripts/10_make_test_pool.sh` | root | crée les 4 images, le pool RAIDZ1, les données connues, la référence, les dumps `zdb`, exporte le pool et **fige les images en 444** |
| `scripts/20_scenario_missing.sh` | user | prépare un scénario « disques manquants » (liens symboliques vers les survivants uniquement) |
| `scripts/90_verify_readonly.sh` | user | contrôle forensic : vérifie que les images sources n'ont pas été modifiées |

## Pool de test créé

- 4 images **sparse** de 512 Mio : `testlab/images/disk{a,b,c,d}.img`
- `zpool create -o ashift=12 ... raidz1 diska diskb diskc diskd`
- pas de chiffrement, pas de dedup, pas de special vdev (hors périmètre)

### Datasets et données

| Dataset | recordsize | compression | contenu |
|---|---|---|---|
| `zrtest/small` | 128K | off | 200 fichiers de 64 o à 3,5 Kio (tiennent dans un seul bloc) |
| `zrtest/docs`  | 128K | lz4 | texte compressible, arborescence `2023..2025/mm/`, fichiers 200 Kio et 2 Mio |
| `zrtest/blobs` | 128K | off | binaires incompressibles 64 Kio / 1 Mio / 7 Mio |
| `zrtest/big`   | 1M   | lz4 | 32 Mio et 64 Mio incompressibles + 6 Mio très compressible (multi-stripes) |
| `zrtest/edge`  | 128K | off | fichier vide, 1 octet, exactement 128 Kio, 128 Kio + 1 |

Un snapshot récursif `@base` est créé (utile pour l'exploitation des snapshots prévue plus tard).

### Artefacts de validation produits

| Fichier | Contenu |
|---|---|
| `testlab/reference/` | copie hors pool des fichiers originaux |
| `testlab/reference_sha256.txt` | sha256 de chaque fichier original |
| `testlab/ground_truth_objects.txt` | `type / inode(= n° d'objet dnode) / taille / chemin` de chaque fichier **tel que ZFS l'a stocké** |
| `testlab/zdb/zdb_l_disk?.txt` | les 4 labels de chaque image (référence pour l'étape 2) |
| `testlab/zdb/zdb_C_config.txt`, `zdb_u_uberblock.txt`, `zdb_d_datasets.txt`, `zdb_dddd_*.txt`, `zdb_mm_metaslabs.txt` | référence OpenZFS pour valider notre parseur |
| `testlab/images_sha256.txt` | empreintes des images figées |

## Scénario cible

```
diska.img = MISSING
diskb.img = MISSING
diskc.img = disponible (lecture seule)
diskd.img = disponible (lecture seule)
```

```bash
bash scripts/20_scenario_missing.sh a b
# -> testlab/scenarios/missing_a_b/{diskc.img,diskd.img,scenario.json}
```

D'autres combinaisons (`a c`, `c d`, …) seront testées : ce qui est récupérable dépend
des colonnes RAIDZ survivantes.

## Attendu théorique avec 2 disques sur 4 en RAIDZ1

- **Irrécupérable** : tout bloc logique dont les données s'étalent sur les 4 colonnes
  (typiquement les blocs de 128 Kio / 1 Mio des gros fichiers) — 2 colonnes perdues,
  1 seule parité.
- **Potentiellement récupérable** :
  - les blocs suffisamment petits pour tenir entièrement dans les colonnes survivantes
    (petits fichiers, blocs compressés courts) ;
  - les blocs dont exactement **une** colonne utile manque (reconstruction par parité) ;
  - une grande partie des **métadonnées**, ZFS écrivant 2 copies (ditto blocks) des
    métadonnées et 3 pour celles du MOS, à des offsets différents donc sur des colonnes
    différentes.

C'est précisément ce que l'outil doit mesurer, sans jamais inventer une donnée absente.

## Limitations connues à ce stade

- Le fichier `big/big_48M_compressible.bin` fait en réalité **6 Mio** (son nom est
  trompeur, le motif généré est simplement très compressible) ; sa taille réelle
  est celle enregistrée dans `reference_sha256.txt`, qui fait foi.

- Images de 512 Mio : petites, donc peu de metaslabs — comportement d'allocation moins
  varié que sur les vrais disques. À compléter plus tard par un pool de test plus grand.
- `ashift=12` uniquement pour l'instant ; l'étape 3 (mapping RAIDZ) devra aussi être
  testée avec `ashift=9`.
- La version de ZFS du **vrai** pool n'est pas encore connue (étape 0 réelle) ; le pool de
  test est créé avec OpenZFS 2.3.x. Si le vrai pool provient d'une version plus ancienne,
  un second pool de test sera nécessaire.
