# Étape 2 — Lecture des labels ZFS et découverte de la topologie

*(correspond aussi à la « première livraison attendue » du CLAUDE.md)*

## Ce que fait l'outil

```
python3 -m zfsrescue labels <image...> [--json rapport.json] [--full-labels] [--sha256]
```

1. ouvre chaque support en **lecture seule** ;
2. lit les **4 labels** de chacun ;
3. vérifie le **checksum SHA-256 auto-porté** de chaque label ;
4. décode le **nvlist XDR** de configuration ;
5. identifie le pool (nom, GUID, état, txg, hôte d'origine, feature flags) ;
6. reconstruit la **topologie RAIDZ1 à 4 colonnes** ;
7. marque explicitement en **MISSING** les vdev dont aucune image n'est fournie ;
8. affiche les **paramètres nécessaires au mapping RAIDZ** (étape 3) ;
9. produit un **rapport JSON** ;
10. n'écrit **rien** sur les sources.

## Structures ZFS utilisées

Toutes recopiées depuis `vendor/zfs-linux-2.3.9/` (source Debian officielle d'OpenZFS 2.3.9).

### `vdev_label_t` — `include/sys/vdev_impl.h:531`

| Décalage | Taille | Champ | Contenu |
|---|---|---|---|
| 0 | 8 Kio | `vl_pad1` | réservé |
| 8 Kio | 8 Kio | `vl_be` | *boot env block* (GRUB) |
| 16 Kio | 112 Kio | `vl_vdev_phys` | nvlist de configuration + `zio_eck_t` |
| 128 Kio | 128 Kio | `vl_uberblock` | anneau d'uberblocks (étape 4) |

Total **256 Kio**. Chaque support porte **4 copies** de ce label.

### Emplacement des labels — `module/zfs/vdev_label.c:163`

```c
return (offset + l * sizeof (vdev_label_t) +
        (l < VDEV_LABELS / 2 ? 0 : psize - VDEV_LABELS * sizeof (vdev_label_t)));
```

- L0 = 0, L1 = 256 Kio (début du support)
- L2 = psize − 512 Kio, L3 = psize − 256 Kio (fin du support)
- `psize` = taille du support **arrondie vers le bas** à 256 Kio (`module/zfs/vdev.c:2229`).

Deux labels au début et deux à la fin : un disque tronqué ou dont le début est
écrasé reste partiellement identifiable.

### Checksum embarqué `zio_eck_t` — `include/sys/zio.h:55`

40 octets en fin de région : `zec_magic` (8) + `zec_cksum` (4 × uint64).

Vérification reproduite à l'identique depuis `module/zfs/zio_checksum.c:363-403` :

1. `zec_magic = 0x0210da7ab10c7a11` (sa lecture donne aussi l'**endianness** de la machine qui a écrit) ;
2. `zec_cksum = (offset_physique_de_la_région, 0, 0, 0)` — c'est le *verifier* : le checksum dépend de **l'endroit** où le bloc est écrit, donc un label déplacé est détecté ;
3. SHA-256 sur la région entière ainsi préparée ;
4. les 4 mots du résultat sont stockés en `BE_64` (`module/zfs/sha2_zfs.c:74`).

### nvlist XDR — `module/nvpair/nvpair.c:3205`

```
en-tête (4 o) : encoding, endian, reserved, reserved
nvl_version (int32) | nvl_nvflag (uint32)
pour chaque couple :
    encoded_size (int32) | decoded_size (int32)
    nom (longueur int32 + octets + padding 4)
    type (int32) | nelem (int32) | données
fin : deux int32 nuls
```

XDR est toujours **big-endian**. Le décodeur utilise `encoded_size` pour se
resynchroniser si un couple est mal interprété, et consigne l'anomalie plutôt
que d'abandonner la lecture.

## Piège identifié et corrigé

`xdr_char()` encode un octet sur 4 octets **avec extension de signe** : une
valeur `uint8` de 200 est écrite `0xFFFFFFC8`. Une lecture naïve en `int32`
renvoie −56. Le décodeur masque donc l'octet de poids faible puis applique le
signe du type déclaré (idem pour `int16` et les tableaux 8/16 bits).

Ce défaut a été trouvé par le test comparatif avec `libnvpair` — pas par
relecture du code. Il n'affectait aucun champ des labels réels (qui n'utilisent
que `uint64`, `string`, `nvlist`), mais il aurait faussé d'autres nvlists ZFS.

## Tests

```bash
bash scripts/30_run_tests.sh          # tests + contrôle d'intégrité des images
# ou :
python3 -m unittest discover -s tests -t . -v
```

**36 tests**, répartis en quatre familles :

| Fichier | Oracle | Ce qui est vérifié |
|---|---|---|
| `test_constants_vs_openzfs.py` | **source OpenZFS** (`vendor/`) | chaque constante est comparée au `#define` réel ; les `#define` sont évalués (macros, décalages, `sizeof(vdev_label_t)` recalculé depuis les membres) ; formule `vdev_label_offset()` ; règle `VDEV_UBERBLOCK_SHIFT` pour ashift 9/12/13/16 |
| `test_nvlist_vs_libnvpair.py` | **libnvpair.so** (OpenZFS) | nvlists réelles encodées par la bibliothèque officielle puis décodées par nous : scalaires signés/non signés, chaînes de toutes longueurs (padding XDR), tableaux, nvlist imbriquées, tableau de nvlists façon `vdev_tree` ; refus des encodages non XDR et des tampons tronqués |
| `test_labels_vs_zdb.py` | **`zdb -l`** | tous les champs de configuration des 4 labels des 4 images ; les 4 labels sont identiques ; offsets attendus ; 16/16 checksums SHA-256 valides ; **détection de corruption** (1 bit modifié en mémoire → checksum invalide) ; **dépendance à l'offset** (même bloc, mauvaise position → invalide) |
| `test_topology.py` | données connues | pool complet (4/4) ; **les 6 combinaisons de 2 disques survivants** ; 1 seul disque ; cohérence `asize` ; fichier sans label ZFS ; mélange image valide + fichier quelconque |
| `test_readonly_guarantee.py` | — | audit statique du code (aucun `O_WRONLY`/`O_RDWR`/`O_CREAT`/`os.write`… hors chaînes et commentaires) ; SHA-256 + horodatages des 4 images identiques avant/après une exécution complète ; refus des lectures hors support |

## Résultats obtenus sur le pool de test

```
pool zrtest  guid=16937172268484420114  version=5000  txg=41  état=EXPORTED
vdev 0 : raidz1  ashift=12  asize=2 128 609 280  (4 colonnes)
   >> col[0] MISSING   guid=1175289873954860442
   >> col[1] MISSING   guid=10206891752548120991
      col[2] AVAILABLE diskc.img   asize=532 152 320   labels valides 4/4
      col[3] AVAILABLE diskd.img   asize=532 152 320   labels valides 4/4
```

Contrôle arithmétique automatique : `asize_total = 4 × asize_par_colonne`, et
`asize_par_colonne = psize − 4 Mio (L0+L1+zone de boot) − 512 Kio (L2+L3)`.

Paramètres transmis à l'étape 3 : `nparity=1`, `ncols=4`, `ashift=12`
(secteur 4096), début de zone allouable `4 194 304` sur chaque feuille,
metaslabs `shift=27` (128 Mio) `array=135` `nombre=15`, uberblocks `32` par
label de `4096` octets.

Rapports produits : `reports/etape2_pool_complet.json`,
`etape2_missing_a_b.json`, `etape2_missing_a_c.json`, `etape2_missing_c_d.json`.

## Limitations connues

- **Aucune lecture de données** à ce stade : ni uberblocks, ni MOS, ni fichiers.
- Un seul vdev de premier niveau est reconstruit : c'est une limite du format,
  le label d'une feuille ne décrit que **son** vdev de premier niveau. Si le
  pool en comptait plusieurs, il faudrait le MOS (étape 5) pour l'arbre complet.
  L'outil émet un avertissement quand `vdev_children` > 1.
- L'étiquette `state: EXPORTED` reflète notre pool de test exporté proprement ;
  un pool réel arraché affichera `ACTIVE` — sans conséquence pour la lecture.
- Seul l'encodage nvlist **XDR** est accepté (le seul utilisé dans les labels).
  Un nvlist natif serait rejeté explicitement, jamais deviné.
- Les labels d'un pool écrit par une machine **big-endian** sont détectés
  (`byteswapped`) mais ce cas n'a pas encore de jeu de test.
- `min_alloc`/`max_alloc` et `com.klarasystems:vdev_zaps_v2` sont propres à ZFS
  ≥ 2.3 ; un pool plus ancien n'aura pas ces champs, ce qui est sans effet sur
  la lecture des labels.

## Vdev situé dans une partition (FreeBSD, FreeNAS/TrueNAS, Linux)

Un vdev n'occupe pas toujours le disque entier. FreeBSD et FreeNAS/TrueNAS
placent le pool dans une partition, typiquement :

```
/dev/sdc1     128    4194431      2G    FreeBSD swap
/dev/sdc2 4194432 3907029127    1,8T    FreeBSD ZFS
```

Les quatre labels sont alors au début et à la fin de **la partition**, pas du
disque. Pointer l'outil sur `/dev/sdc` donne donc « 0/4 labels valides ».

L'outil gère ce cas seul :

1. il cherche d'abord les labels sur le support entier ;
2. s'il n'en trouve aucun, il lit la table de partitions (**GPT** avec
   vérification des deux CRC, ou **MBR**) ;
3. il essaie les partitions candidates, celles de type ZFS d'abord
   (`FreeBSD ZFS`, `Solaris /usr`, `Solaris root`, MBR `0xBF`/`0xA5`) ;
4. il ne retient une partition **que si de vrais labels valides s'y trouvent**,
   et le signale dans le rapport :

```
  /dev/sdc
      VDEV DANS UNE PARTITION : n°2 a l'offset 2147483648 (FreeBSD ZFS)
      support entier : 2000398934016 octets (1.82 Tio)
      note : labels ZFS trouves dans la partition 2 : offset 2147483648, ...
```

Si rien n'est trouvé nulle part, la table de partitions est affichée telle
quelle, les candidates ZFS marquées, pour orienter la suite :

```
      table de partitions GPT (secteur 512) :
        n°1  offset        1048576      2.00 Gio  FreeBSD swap
        n°2  offset     2147483648      1.82 Tio  FreeBSD ZFS  <-- candidate ZFS
```

Deux options pour les cas particuliers :

| Option | Usage |
|---|---|
| `--offset OCTETS` | force le début du vdev — utile si la table de partitions est détruite |
| `--no-partition-scan` | s'en tient au support entier |

Cette recherche fonctionne aussi bien sur un périphérique bloc que sur une
**image de disque entier** : inutile d'extraire la partition avant l'analyse.
