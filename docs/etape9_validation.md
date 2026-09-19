# Étape 9 — Validation

Deux outils, deux portées.

## 1. Validation d'une extraction

```bash
python3 scripts/50_validate_recovery.py \
        --report reports/etape7_extraction.json \
        --extracted testlab/extracted
```

Vérifie que **l'outil ne ment pas** :

| Contrôle | Règle |
|---|---|
| fichiers `COMPLET` | sha256 identique à l'original, **et** fichier écrit identique |
| fichiers `PARTIEL` | pour chaque bloc déclaré récupéré, les octets écrits doivent être identiques à ceux de l'original **aux mêmes positions** |
| fichiers `PERDU` | ne doivent rien revendiquer |
| tailles annoncées | comparées aux tailles réelles |
| fichiers d'origine absents du rapport | comptés comme non retrouvés |

Résultat sur l'extraction « colonnes 2+3 » :

```
fichiers declares COMPLET et verifies identiques : 103
fichiers declares COMPLET mais DIFFERENTS        : 0
fichiers PARTIELS verifies (octets recuperes ok) : 2
fichiers PARTIELS avec des octets faux           : 0
fichiers declares PERDU                          : 110
fichiers d'origine non retrouves par l'outil     : 42
SUCCES : aucune donnee declaree recuperee n'est fausse.
```

## 2. Matrice des six scénarios

```bash
python3 scripts/60_scenarios_matrix.py --json reports/etape9_matrice_scenarios.json
```

Rejoue les 6 combinaisons de 2 disques survivants sur 4 et compare chaque
empreinte annoncée à celle du fichier d'origine.

| Survivants | Colonnes | Complets | Partiels | Perdus | Total | Blocs reconstruits | Octets | Faux |
|---|---|---|---|---|---|---|---|---|
| a+b | 0,1 | 97 | 2 | 94 | 193 | 0 | 50,7 % | **0** |
| a+c | 0,2 | 249 | 0 | 7 | 257 | 158 | 8,4 % | **0** |
| a+d | 0,3 | 248 | 0 | 8 | 257 | 132 | 3,1 % | **0** |
| b+c | 1,2 | 249 | 0 | 7 | 257 | 135 | 8,4 % | **0** |
| b+d | 1,3 | 248 | 0 | 8 | 257 | 115 | 3,1 % | **0** |
| c+d | 2,3 | 103 | 2 | 110 | 215 | 0 | 49,1 % | **0** |

Lecture de ce tableau :

- **Paires décalées** (a+c, a+d, b+c, b+d) : presque tous les **fichiers** sont
  récupérés (248–249 sur 257) grâce à la reconstruction par la parité, mais le
  pourcentage d'**octets** est faible parce que les très gros fichiers du jeu de
  test (32 et 64 Mio incompressibles) sont listés et comptés au dénominateur
  tout en étant irrécupérables.
- **Paires alignées** (a+b, c+d) : aucune reconstruction possible, la moitié des
  petits fichiers survit, et les datasets `big`/`blobs` ne sont même plus
  listables — d'où un total de fichiers plus faible (193 et 215 au lieu de 257)
  et, paradoxalement, un pourcentage d'octets plus élevé : le dénominateur
  n'inclut plus les fichiers qu'on ne voit pas.

Les deux chiffres sont donc à lire ensemble ; aucun ne résume à lui seul la
situation.

## Anomalie trouvée par cette validation

Le premier passage de la matrice a signalé 4 fichiers « déclarés récupérés mais
différents ». Enquête : il s'agissait d'un **défaut du rapport**, pas des
données — en mode analyse seule (`--dry-run`), un fichier de taille nulle
n'avait pas d'empreinte renseignée. Corrigé, et couvert par un test de
non-régression.

C'est l'intérêt d'une validation qui compare aux originaux plutôt que de faire
confiance au code.

## Couverture de tests

`bash scripts/30_run_tests.sh` — **108 tests**, tous verts, puis contrôle
d'intégrité des images sources.

| Fichier | Portée |
|---|---|
| `test_constants_vs_openzfs.py` | constantes comparées aux `#define` du source |
| `test_nvlist_vs_libnvpair.py` | décodeur nvlist contre `libnvpair.so` |
| `test_labels_vs_zdb.py` | labels contre `zdb -l`, corruption, offsets |
| `test_topology.py` | topologie, 6 combinaisons de disques, cas dégradés |
| `test_raidz_map.py` | mapping RAIDZ : valeurs de référence, invariants, `zdb -R` |
| `test_uberblocks.py` | uberblocks contre `zdb -lu`, règles de format |
| `test_mos.py` | objets du MOS contre `zdb -dddd`, datasets contre `zdb -d` |
| `test_checksums_compression.py` | fletcher/sha256, lz4/zle/lzjb, blocs réels |
| `test_zpl_extraction.py` | navigation, inférence SA, extraction bit à bit |
| `test_generated_enums.py` | énumérations synchronisées avec le source |
| `test_readonly_guarantee.py` | audit statique + empreintes avant/après |
