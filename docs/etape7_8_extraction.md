# Étapes 7 et 8 — Extraction et fichiers partiellement récupérables

```bash
# analyse sans rien écrire
python3 -m zfsrescue extract testlab/scenarios/missing_a_b/disk?.img --dry-run

# extraction réelle vers un autre support
python3 -m zfsrescue extract testlab/scenarios/missing_a_b/disk?.img \
        --dest /media/sauvegarde/recup --blocks --json rapport.json
```

Options : `--dataset` (un seul dataset), `--txg` (état historique),
`--no-fill` (arrêter un fichier au premier bloc manquant au lieu de combler),
`--skip-incomplete` (n'écrire que les fichiers intégralement récupérés),
`--blocks` (état de chaque bloc dans le JSON).

## État de chaque bloc

| État | Signification |
|---|---|
| `RECOVERED` | lu directement, **checksum validé** |
| `RECOVERED_WITH_RECONSTRUCTION` | une colonne reconstruite par la parité, **checksum validé** |
| `INVALID_CHECKSUM` | données obtenues mais checksum faux → refusées |
| `MISSING_DATA` | colonnes absentes : irrécupérable |
| `UNKNOWN` | cas non géré (gang, chiffrement, compression indisponible) |

Aucun bloc n'est accepté sans validation du checksum. Une colonne absente reste
absente : elle n'est jamais remplacée par des zéros *dans le calcul*.

## État de chaque fichier

| État | Règle |
|---|---|
| `COMPLET` | **tous** les blocs valides |
| `PARTIEL` | au moins un bloc valide et au moins un manquant |
| `PERDU` | aucun bloc valide |
| `VIDE` | fichier de taille nulle (contenu connu) |

Un fichier partiel est écrit avec des zéros à la place des blocs manquants
(sauf `--no-fill`), mais ces octets ne sont **jamais comptés** comme récupérés,
et le rapport donne la position exacte de chaque trou :

```
fichier.iso
  bloc 0   RECOVERED     offset 0        4096 o
  bloc 1   RECOVERED     offset 4096     4096 o
  bloc 2   MISSING_DATA  offset 8192     4096 o
  bloc 3   RECOVERED     offset 12288    4096 o
```

`ResultatFichier.carte()` en donne une vue compacte (`.` valide, `r`
reconstruit, `X` perdu, `?` checksum faux).

## Sécurités

- la destination ne peut pas être le répertoire des images sources ;
- un chemin reconstruit qui sortirait du répertoire de destination est refusé ;
- `(parent perdu)` est transformé en `_parent_perdu` dans l'arborescence écrite ;
- les sources sont ouvertes en `O_RDONLY` : `scripts/90_verify_readonly.sh`
  revérifie les empreintes après chaque exécution.

## Résultats sur le pool de test

Colonnes 2 et 3 (paire alignée) :

```
zrtest/docs      47 fichiers  COMPLET=21 PARTIEL=2 PERDU=24   49,2 % des octets
zrtest/small    168 fichiers  COMPLET=82            PERDU=86  47,5 % des octets
TOTAL           215 fichiers  COMPLET=103 PARTIEL=2 PERDU=110
```

Colonnes 0 et 2 (paire décalée) :

```
zrtest/small    200 fichiers  COMPLET=200  (100 %)
zrtest/docs      47 fichiers  COMPLET=47   (100 %)
zrtest/big        3 fichiers  COMPLET=1  PERDU=2
zrtest/blobs      3 fichiers  PERDU=3
TOTAL           257 fichiers  COMPLET=249  PERDU=7  VIDE=1
                158 blocs reconstruits par la parité
```

Le seul gros fichier récupéré (`big_48M_compressible.bin`, 6 Mio de motifs
répétitifs) l'est parce que **la compression réduit son `psize`** : ses blocs
n'occupent plus toutes les colonnes. C'est une règle générale : sur un RAIDZ1
amputé de deux disques, **un bloc compressé a bien plus de chances de survivre
qu'un bloc incompressible**.

## Limitations connues

- Les blocs *gang* ne sont pas suivis (état `UNKNOWN`).
- Les trous (`holes`) sont restitués comme des zéros : c'est leur contenu réel.
- L'extraction ne restaure ni les permissions, ni les dates, ni les xattr sur
  les fichiers écrits — seulement le contenu (volontaire : le rapport JSON
  porte les métadonnées).
- La vitesse est limitée par fletcher-4 en Python pur (~24 ms par bloc de
  128 Kio). Pour un vrai pool de plusieurs téraoctets, il faudra accélérer
  (numpy ou extension C) ; les lectures elles-mêmes ne sont pas le facteur
  limitant.
