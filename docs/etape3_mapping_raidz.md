# Étape 3 — Mapping RAIDZ

Étape la plus critique : elle détermine, pour chaque bloc logique, **quelles
colonnes** portent quoi et **à quel offset physique** sur chaque disque. Toute
erreur ici produirait des données plausibles mais fausses.

## Implémentation

`zfsrescue/raidz.py` est un portage ligne à ligne de
`vdev_raidz_map_alloc()` — `module/zfs/vdev_raidz.c:585` d'OpenZFS 2.3.9. Les
noms de variables du source (`b, s, f, o, q, r, bc, tot, acols, scols`) sont
conservés pour permettre la relecture comparée.

```
b = offset >> ashift            secteur de départ dans le vdev RAIDZ
s = taille >> ashift            nombre de secteurs de données
f = b % dcols                   première colonne du bloc
o = (b / dcols) << ashift       offset de départ sur chaque enfant

q  = s / (dcols - nparity)      secteurs par colonne « normale »
r  = s - q * (dcols - nparity)  reste
bc = r ? r + nparity : 0        colonnes « larges » (q+1 secteurs)
tot = s + nparity * (q + (r ? 1 : 0))
```

Les `nparity` premières colonnes de la rangée portent la parité, les suivantes
les données **dans l'ordre des colonnes** (`vdev_raidz_map_alloc_read`,
vdev_raidz.c:557). La concaténation des colonnes de données restitue exactement
`psize` octets.

Trois règles faciles à manquer, toutes vérifiées par des tests :

| Règle | Source | Effet |
|---|---|---|
| Offset physique = offset colonne **+ 4 Mio** | `zio.c:1684`, `zio_vdev_child_io()` | les DVA sont relatifs à la zone allouable, pas au début du disque |
| Taille d'E/S = `psize` **arrondi au secteur** | `zio.c:4460-4472` | un bloc de 512 o occupe un secteur entier sur un vdev `ashift=12` |
| Permutation parité/donnée tous les 1 Mio | `vdev_raidz.c:695` | pour raidz1 **uniquement**, si le bit `1<<20` de l'offset est armé, les colonnes 0 et 1 sont échangées. Le commentaire du source parle d'une contrainte de format « for all eternity » |

Hors périmètre assumé : `vdev_raidz_map_alloc_expanded()` (pools ayant subi une
expansion RAIDZ). L'état de reflow se lit dans l'uberblock ; il sera contrôlé à
l'étape 4 et le traitement sera refusé explicitement plutôt que deviné.

## Validation

### 1. Comparaison différentielle avec `zdb -R`

`zdb -R pool 0:offset:taille:r` restitue un bloc en utilisant le mapping
d'OpenZFS. Nos octets — assemblés à partir de lectures brutes des 4 images —
doivent être identiques.

```bash
python3 scripts/31_raidz_sweep.py        # campagne exhaustive (~4 min)
```

**240 combinaisons** (15 offsets × 16 tailles, de 4 Kio à 1 Mio, avec et sans
bit `1<<20`, avec bouclage de colonnes) : **240 identiques, 0 écart**.

Un sous-ensemble de 64 combinaisons est rejoué par la suite de tests.

### 2. Invariants du format

Balayage de `ashift ∈ {9,12,13}` × `(dcols,nparity) ∈ {(3,1),(4,1),(5,1),(8,1),(6,2),(9,3)}`
× 45 tailles × 9 offsets :

- `Σ tailles de colonnes = asize = tot << ashift` ;
- `Σ colonnes de données = psize` exactement ;
- aucune colonne physique utilisée deux fois ;
- les offsets ne prennent que deux valeurs, `o` et `o + secteur` ;
- `dva_asize` calculé par le mapping **=** `vdev_raidz_asize()` porté séparément ;
- `dva_asize` toujours multiple de `(nparity+1) × secteur`.

### 3. Confrontation aux vrais block pointers

`vdev_raidz_asize()` confronté aux blocs réels du pool de test :

| psize | asize calculé | asize lu par `zdb` |
|---|---|---|
| 128 Kio | `0x2c000` | `L0 0:3562000:2c000 20000L/20000P` |
| 1 Mio | `0x156000` | `L0 0:382000:156000 100000L/100000P` |

Sur les **1 636 blocs** du pool, l'`asize` de chaque DVA correspond au calcul :
aucune divergence.

## Ce que le mapping révèle sur la récupérabilité

`scripts/32_dva_stats.py` applique le mapping aux vrais block pointers et
détermine, sans rien deviner, ce qui reste lisible.

### Le point clé : les allocations sont alignées

`roundup(tot, nparity+1)` impose qu'une allocation RAIDZ occupe toujours un
multiple de `nparity+1` secteurs. Sur un raidz1, **tout bloc commence donc à un
secteur pair**, ce qui a été vérifié sur les 1 636 blocs du pool (f ∈ {0, 2},
jamais 1 ni 3).

Conséquence directe : un petit bloc (≤ 1 secteur de données) occupe toujours la
paire de colonnes **{0,1} ou {2,3}**, jamais à cheval.

### Quelle paire survit change tout

```
python3 scripts/32_dva_stats.py --compare-all-pairs
```

| Colonnes survivantes | Blocs lisibles | dont par reconstruction | Octets L0 lisibles |
|---|---|---|---|
| {0,1} | 37,2 % | 19 | 0,4 % |
| {0,2} | **69,9 %** | 638 | 0,8 % |
| {0,3} | **68,8 %** | 554 | 0,7 % |
| {1,2} | **69,9 %** | 546 | 0,8 % |
| {1,3} | **68,8 %** | 505 | 0,7 % |
| {2,3} | 34,3 % | 0 | 0,4 % |

Deux régimes très différents :

- **paire alignée** ({0,1} ou {2,3}) : un petit bloc est soit entièrement sur
  les survivants, soit entièrement perdu. La parité ne sert presque jamais ;
- **paire décalée** ({0,2}, {0,3}, {1,2}, {1,3}) : de nombreux blocs n'ont
  qu'une colonne utile manquante et redeviennent **reconstructibles par la
  parité** — le double de blocs lisibles.

Autrement dit, pour le vrai pool, **l'identité des deux disques perdus
(leur position dans le vdev, pas leur numéro de série) conditionne largement le
résultat**. Le rapport de l'étape 2 donne cette position (`col[N]`).

### En octets, l'image est sévère

Seuls ~0,4 à 0,8 % des **octets** de données de fichiers sont lisibles, parce
que les gros enregistrements (128 Kio, 1 Mio) s'étalent sur les 4 colonnes et
sont perdus sans exception. Ce qui survit : les petits fichiers, les fins de
fichiers, les blocs compressés courts, et surtout les **métadonnées**, écrites
en 2 ou 3 copies à des offsets différents.

Ce chiffre dépend entièrement du contenu : notre pool de test est volontairement
dominé par de très gros fichiers. Un pool de documents ou de petits fichiers
donnerait un tout autre résultat. Ne pas extrapoler ce pourcentage au vrai pool.

## Utilisation

```bash
# mapping d'un bloc, avec verdict de récupérabilité
python3 -m zfsrescue raidz-map --dva 0:1a16c000:2000 --psize 0x1000 \
        --from-report reports/etape2_missing_a_b.json
```

```
  idx  role     colonne(devidx)  offset allouable   offset physique   taille
    0  parity                1         109424640         113618944     4096  [MISSING]
    1  data                  0         109424640         113618944     4096  [MISSING]
  ETAT DU BLOC : MISSING_DATA
```

`--from-report` reprend `ashift`, `ncols`, `nparity` et les colonnes disponibles
du rapport d'étape 2. Sinon : `--ashift --ncols --nparity --available`.

États possibles d'un bloc, sans complaisance :

| État | Signification |
|---|---|
| `COMPLETE` | toutes les colonnes de données sont lisibles |
| `RECONSTRUCTIBLE` | colonnes de données manquantes ≤ colonnes de parité disponibles |
| `MISSING_DATA` | irrécupérable avec ces supports — jamais comblé par des zéros |

Une colonne dont le support est absent vaut `None`, jamais des zéros : rien
n'est inventé.

## Limitations connues

- La **reconstruction effective** par parité (XOR) n'est pas encore implémentée :
  l'étape 3 calcule le mapping et le verdict, l'étape 7 produira les octets
  reconstruits, validés par checksum.
- Expansion RAIDZ (`vdev_raidz_map_alloc_expanded`) non gérée — à contrôler via
  l'uberblock à l'étape 4.
- Les blocs *gang* (bloc fragmenté en plusieurs allocations) ne sont pas encore
  traités ; ils sont rares et seront détectés via le drapeau `G` du block pointer.
- La campagne différentielle avec `zdb -R` a été menée avec `ashift=12` et
  4 colonnes uniquement, faute d'autres pools de test. Les autres géométries ne
  sont couvertes que par les invariants.
