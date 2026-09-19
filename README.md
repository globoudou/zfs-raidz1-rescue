# zfsrescue

Lecteur forensic ZFS **strictement en lecture seule**, dédié au scénario
RAIDZ1 de 4 vdev dont 2 sont définitivement perdus.

Objectif : récupérer tout ce qui est **réellement** récupérable depuis les
deux disques survivants — jamais reconstruire un pool, jamais deviner une
donnée absente. Voir `CLAUDE.md` pour le cahier des charges complet.

## État d'avancement

| Étape | Sujet | État |
|---|---|---|
| 0 | Analyse de l'environnement réel (vrais disques) | en attente des images |
| 1 | Environnement de test reproductible | **fait** — `docs/etape1_environnement_de_test.md` |
| 2 | Parseur de labels + topologie + JSON | **fait** — `docs/etape2_labels.md` |
| 3 | Mapping RAIDZ | **fait** — `docs/etape3_mapping_raidz.md` |
| 4 | Uberblocks | **fait** — `docs/etape4_uberblocks.md` |
| 5 | MOS | **fait** — `docs/etape5_mos.md` |
| 6 | Navigation (datasets, répertoires, fichiers) | **fait** — `docs/etape6_navigation.md` |
| 7–8 | Extraction, fichiers partiels | **fait** — `docs/etape7_8_extraction.md` |
| 9 | Validation | **fait** — `docs/etape9_validation.md` |
| 10 | Application aux vraies images | **procédure prête** — `docs/etape10_procedure_reelle.md` |

## Utilisation

```bash
python3 -m zfsrescue labels testlab/scenarios/missing_a_b/disk?.img \
        --json reports/mon_rapport.json
```

```bash
# état du pool, uberblocks, MOS, datasets
python3 -m zfsrescue uberblocks  <images...>
python3 -m zfsrescue mos         <images...>

# fichiers récupérables, puis extraction vers un autre support
python3 -m zfsrescue ls      <images...> [--dataset zrtest/docs]
python3 -m zfsrescue extract <images...> --dest /media/destination [--dry-run]

# mapping RAIDZ d'un bloc + verdict de récupérabilité
python3 -m zfsrescue raidz-map --dva 0:1a16c000:2000 --psize 0x1000 \
        --from-report reports/etape2_missing_a_b.json
```

Options de `labels` : `--full-labels` (config complète de chaque label dans le JSON),
`--sha256` (empreinte de chaque support), `-q` (JSON seulement),
`--json-stdout`.

Code de retour : `0` si tout est cohérent, `1` si des incohérences ont été
détectées, `2` en cas d'erreur d'accès.

## Organisation

```
zfsrescue/      le code :
                  readonly, nvlist, label, topology       (étapes 2)
                  raidz                                   (étape 3)
                  blkptr, uberblock                       (étape 4)
                  checksum, compress, blockio, dmu, zap, dsl, pool  (étape 5)
                  zpl                                     (étape 6)
                  extract                                 (étapes 7-8)
                  zfs_enums (généré), consts, report, cli
tests/          la suite de tests + les oracles (zdb, libnvpair)
scripts/        installation ZFS, création du pool de test, scénarios, contrôles
vendor/         source OpenZFS 2.3.9 — référence normative des structures binaires
testlab/        images de test, données de référence, dumps zdb, scénarios
reports/        rapports produits
docs/           documentation par étape
```

## Reproduire l'environnement de test

```bash
sudo bash scripts/00_setup_zfs_host.sh     # installe OpenZFS (machine de dev)
sudo bash scripts/10_make_test_pool.sh     # crée disk{a,b,c,d}.img + le pool + les données
bash  scripts/20_scenario_missing.sh a b   # scénario : 2 disques détruits
bash  scripts/30_run_tests.sh              # tests + contrôle d'intégrité
python3 scripts/31_raidz_sweep.py          # campagne différentielle RAIDZ contre zdb
python3 scripts/32_dva_stats.py --compare-all-pairs   # récupérabilité des vrais blocs
python3 scripts/50_validate_recovery.py --report reports/etape7_extraction.json
python3 scripts/60_scenarios_matrix.py                # les 6 scénarios, validés
```

## Garanties de lecture seule

- ouverture des supports en `O_RDONLY` (+ `O_NOATIME` quand c'est permis) ;
- lectures par `pread()` uniquement, aucune primitive d'écriture dans les
  modules d'accès — vérifié par un test d'audit statique ;
- images de test figées en mode `444`, empreintes SHA-256 contrôlées avant et
  après chaque exécution (`scripts/90_verify_readonly.sh`) ;
- la seule écriture de l'outil est le fichier de rapport demandé par `--json`.

## Licence et origine du code

Projet distribué sous **CDDL-1.0**, comme OpenZFS. Ce n'est pas une
réimplémentation indépendante : plusieurs modules sont des portages directs de
code OpenZFS et toutes les structures binaires proviennent de ses en-têtes. Le
détail fichier par fichier est dans `NOTICE`.

Projet non affilié à OpenZFS.

## Installation et prérequis

```bash
git clone https://github.com/<votre-compte>/zfs-raidz1-rescue.git
cd zfs-raidz1-rescue
python3 -m zfsrescue --version        # aucune dépendance obligatoire
```

Python ≥ 3.10, aucune dépendance obligatoire. Optionnel :

| Paquet | Utilité |
|---|---|
| `python3-zstandard` | lire les blocs compressés en zstd (sinon l'outil refuse explicitement) |
| `zfsutils-linux` | fournit `zdb`, utilisé comme oracle par une partie des tests |
| `libnvpair3linux` | utilisé comme oracle par les tests nvlist |

### Source OpenZFS de référence

Le dépôt ne contient pas le source d'OpenZFS. Les tests qui le comparent aux
constantes du projet le cherchent dans `vendor/` et se contentent de se
désactiver s'il est absent. Pour l'installer :

```bash
sudo sed -i -E 's/ main( |$)/ main contrib\1/' /etc/apt/sources.list   # Debian
sudo apt update
mkdir -p vendor && cd vendor && apt-get source zfs-linux
```

### Environnement de test

Le dépôt ne contient pas non plus les images du pool de test (2 Gio) ni les
données de référence : elles se recréent à l'identique avec
`scripts/10_make_test_pool.sh` (voir « Reproduire l'environnement de test »).

## Avertissement

Cet outil lit des systèmes de fichiers endommagés. Il est conçu pour ne jamais
écrire sur ses sources, et cette propriété est vérifiée par des tests, mais
**faites toujours des images bit-à-bit et travaillez sur les copies**. Voir
`docs/etape10_procedure_reelle.md`.

Aucune garantie : la récupération de données comporte une part irréductible
d'incertitude. L'outil s'efforce de distinguer clairement ce qui est vérifié
de ce qui est supposé — lisez les rapports avant de vous fier aux résultats.
