# Étape 4 — Lecture des uberblocks

## Ce que fait l'outil

```bash
python3 -m zfsrescue uberblocks testlab/scenarios/missing_a_b/disk?.img \
        --json reports/etape4.json
```

Pour chaque support fourni : parcours des **4 anneaux d'uberblocks** (un par
label), décodage de chaque créneau, validation du checksum auto-porté,
décodage du `blkptr` racine pointant vers le MOS, puis agrégation en un
historique des états du pool potentiellement exploitables.

## Structures utilisées

### `struct uberblock` — `include/sys/uberblock_impl.h:122`

| Décalage | Champ | Rôle |
|---|---|---|
| 0 | `ub_magic` | `0x00bab10c` — sert aussi à détecter l'endianness |
| 8 | `ub_version` | 5000 = feature flags |
| 16 | `ub_txg` | groupe de transactions |
| 24 | `ub_guid_sum` | **somme des GUID de tous les vdev de l'arbre** |
| 32 | `ub_timestamp` | date de la synchronisation (epoch UTC) |
| 40 | `ub_rootbp` | `blkptr_t` (128 o) → objset du MOS |
| 168 | `ub_software_version` | |
| 176..200 | `ub_mmp_*` | multihost |
| 200 | `ub_checkpoint_txg` | non nul = uberblock de checkpoint |
| 208 | `ub_raidz_reflow_info` | état d'expansion RAIDZ |

216 octets ; le reste du créneau est mis à zéro avant écriture
(`vdev_label.c:1797`).

### Anneau d'uberblocks

- Taille d'un créneau : `2^MIN(MAX(ashift,10),13)` — **4096 o** pour `ashift=12`
  (`vdev_impl.h:487`) ;
- nombre de créneaux : `128 Kio / taille` — **32** ;
- créneau utilisé : **`txg % nombre_de_créneaux`** (`vdev_label.c:1793`),
  moins un créneau si MMP est actif ;
- checksum : SHA-256 auto-porté, verifier = offset physique du créneau, comme
  pour le nvlist du label.

### `blkptr_t` — `include/sys/spa.h:373`

Décodeur complet (`zfsrescue/blkptr.py`) : 3 DVA (vdev / offset / asize / gang),
`lsize`/`psize`, compression, checksum, type DMU, niveau, endianness, dedup,
chiffrement, naissances logique et physique, fill, checksum 256 bits, plus les
**blkptr embarqués** (données stockées dans le pointeur lui-même).

Les tables d'énumérations (types DMU, algorithmes de compression et de
checksum) ne sont **pas recopiées à la main** : `scripts/40_gen_enums.py` les
extrait des en-têtes OpenZFS vers `zfsrescue/zfs_enums.py`, et un test vérifie
que le fichier est à jour.

## Résultats sur les 2 disques survivants

```
Créneaux examinés 256, portant un magic 160, valides 160, corrompus 0
txg 41  2026-09-18T21:43:17Z  créneau 9 (= 41 % 32)
somme des guid : 6610987066146725634 (cohérente)
checkpoint : 0      expansion RAIDZ : état=0 (NOT_IN_USE)
MOS : 0:1a19e000:2000 0:83ac000:2000 0:200ac000:2000 [L0 objset]
      fletcher_4 off size=1000L/1000P birth=41 fill=93   → 3 copies
20 états historiques disponibles (txg 41 à 4)
```

Trois points importants :

1. **Les uberblocks survivent intégralement à la perte de 2 disques** : ils
   vivent dans les labels, répliqués 4 fois par disque. Un test vérifie que la
   liste obtenue avec 2 disques est identique à celle obtenue avec 4.
2. **`ub_guid_sum` est un contrôle d'exhaustivité** : la somme des GUID connus
   (racine + vdev de premier niveau + feuilles, `vdev.c:573`) correspond
   exactement. Une divergence signifierait qu'il existe des vdev que les labels
   disponibles ne décrivent pas — l'outil le signale explicitement.
3. **Le MOS a 3 copies** (ditto blocks), à trois offsets très différents. C'est
   ce qui rend l'étape 5 possible malgré 2 disques manquants.

## Vérifications

| Test | Nature |
|---|---|
| liste identique à `zdb -lu` | créneaux occupés, txg, version, guid_sum, timestamp, checkpoint, état de reflow, labels concernés |
| `rootbp` identique à `zdb` | DVA (vdev/offset/asize), checksum 256 bits, lsize/psize, compression, checksum, endianness |
| créneau = `txg % 32` | règle de format vérifiée sur tous les uberblocks valides |
| les 4 labels portent les mêmes uberblocks | cohérence interne |
| `ub_guid_sum` | recalculé depuis la configuration des labels |
| corruption | 1 octet modifié en mémoire → uberblock invalidé (4 positions testées) |
| créneau vide / big-endian | jamais considérés valides, jamais devinés |
| 2 disques = 4 disques | même ensemble d'uberblocks, même meilleur état |

**14 tests**, tous verts. Suite complète : 74 tests.

## Limitations connues

- L'outil **n'exploite pas encore** les états historiques : il les liste. Le
  retour à un txg antérieur (utile si le txg le plus récent pointe vers des
  métadonnées perdues) sera possible à l'étape 5 en choisissant l'uberblock.
- MMP, checkpoint et expansion RAIDZ sont **détectés et signalés**, pas gérés.
- Un uberblock big-endian est reconnu mais son contenu n'est pas décodé (aucun
  jeu de test disponible).
