# Étape 5 — Lecture du MOS

```bash
python3 -m zfsrescue mos testlab/scenarios/missing_a_b/disk?.img --json rapport.json
```

## Chaîne de lecture mise en place

```
uberblock.rootbp
   │  blkptr.py    décodage du block pointer (3 DVA, lsize/psize, compression,
   │               checksum, type, niveau, naissance, fill)
   ├─ raidz.py     mapping de chaque DVA vers ses colonnes
   ├─ blockio.py   lecture des colonnes, reconstruction XOR si une seule manque,
   │               vérification du checksum, décompression
   ├─ dmu.py       objset_phys_t, dnode_phys_t, descente dans les blocs indirects
   ├─ zap.py       micro-ZAP et fat ZAP (annuaires)
   └─ dsl.py       hiérarchie des datasets
```

## Structures (toutes vérifiées sur le source OpenZFS 2.3.9)

| Structure | Référence | Points clés |
|---|---|---|
| `blkptr_t` | `spa.h:373`, macros `spa.h:391-520` | 3 DVA, `lsize`/`psize` en unités de 512, bits de compression (32-38), embedded (39), checksum (40-47), type (48-55), niveau (56-60), endianness (63) ; naissance physique/logique depuis 2.2 |
| `objset_phys_t` | `dmu_objset.h:80` | `os_meta_dnode` (512 o), `zil_header` (192 o), `os_type` à l'offset 704 |
| `dnode_phys_t` | `dnode.h:219` | 512 o (+ `dn_extra_slots`), `dn_nblkptr` blkptr à l'offset 64, puis le bonus, spill en fin |
| micro-ZAP | `zap_impl.h:53-68` | en-tête 64 o, entrées de 64 o (valeur, cd, nom de 50 o) |
| fat ZAP | `zap_impl.h:118`, `zap_leaf.h` | en-tête bloc 0, feuilles `ZBT_LEAF` : table de hachage puis chunks de 24 o ; **valeurs stockées en big-endian** (`zap_leaf.c:355`) |
| `dsl_dir_phys_t` | `dsl_dir.h:69` | dans le **bonus** d'un objet ZAP |
| `dsl_dataset_phys_t` | `dsl_dataset.h:140` | 16 champs puis `ds_bp` (l'objset du dataset) |

Les énumérations (types DMU, compression, checksum) sont **générées** depuis les
en-têtes par `scripts/40_gen_enums.py` ; un test vérifie qu'elles sont à jour.

## Checksums et décompression

| Élément | Référence | État |
|---|---|---|
| fletcher-2 / fletcher-4 | `zfs_fletcher.c:238,318` | implémentés, validés sur tous les blocs du MOS |
| SHA-256 | `sha2_zfs.c:44` (mots en `BE_64`) | implémenté |
| sha512 / skein / edonr / blake3 | — | **non implémentés** : erreur explicite, jamais de validation par défaut |
| LZ4 | `lz4.c` (préfixe 4 o big-endian) | implémenté, validé contre les fichiers d'origine |
| LZJB, ZLE, gzip | `lzjb.c:106`, `zle.c:70`, zlib | implémentés |
| zstd | `zfs_zstd.c` (en-tête 8 o) | supporté **si** `python3-zstandard` est installé, sinon erreur explicite |

## Stratégie de lecture d'un bloc

1. essai de chaque DVA (copie) dans l'ordre ;
2. mapping RAIDZ → lecture des colonnes présentes ;
3. si exactement une colonne de données manque et que la parité est là :
   reconstruction XOR (`vdev_raidz.c:1114`) ;
4. vérification du checksum : **un bloc n'est jamais accepté sans elle** ;
5. décompression ;
6. si une copie échoue, on passe à la suivante.

## Résultats avec les 2 disques survivants (colonnes 2 et 3)

```
MOS : 93 objets annoncés (fill), 93 retrouvés, 0 dnode illisible
tableau des dnodes : 13 blocs de 16 Kio, 2 niveaux
annuaire d'objets (objet 1) : ZAP fat, 16/16 entrées, complet
hiérarchie : zrtest + 5 datasets + $MOS/$FREE/$ORIGIN, snapshots inclus
```

Le MOS survit **intégralement** parce que ses blocs ont 3 copies (ditto blocks)
à des offsets très éloignés : au moins une copie tombe sur les colonnes
survivantes.

## Vérifications (10 tests)

| Test | Oracle |
|---|---|
| chaque objet du MOS : niveaux, taille de bloc, bloc indirect, `maxblkid`, taille du bonus, type | `zdb -dddd` |
| nombre d'objets = champ `fill` de l'uberblock | interne |
| annuaire d'objets complet (16/16) | interne + `zdb` |
| arborescence des datasets : nom, numéro d'objet, txg de création | `zdb -d` |
| snapshots : nom → objet | `zdb -d` |
| résultat identique avec 2 disques et avec 4 | interne |
| choix d'un txg historique (`--txg`) | interne |
| un bloc dont les colonnes manquent est déclaré `MISSING_DATA`, jamais reconstitué | interne |

## Limitations connues

- Les blocs *gang* sont détectés et refusés (non gérés).
- Les pools chiffrés sont détectés et refusés.
- Le cache L2ARC, les special vdev et le dedup ne sont pas traités.
