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

## Où atterrissent les fichiers

Rencontré en conditions réelles : un fichier dont **aucun** bloc n'est lisible
était tout de même écrit, entièrement nul. Même nom, même taille, rien pour le
distinguer d'un fichier valide tant qu'on ne l'ouvre pas. C'est exactement ce
qu'un outil de récupération ne doit pas faire.

```
<destination>/<dataset>/...       fichiers INTEGRALEMENT verifies par checksum
<destination>/_partiels/<ds>/...  fichiers incomplets (trous combles de zeros)
<destination>/_conflits/...       chemin reconstitue impossible, ecrit a plat
                                  (rien)  fichiers dont aucun bloc n'est lisible
```

| Option | Effet |
|---|---|
| `--ecrire-perdus` | écrit quand même les fichiers sans aucun bloc lisible — ils seront nuls |
| `--melanger` | remet les partiels dans l'arborescence principale |
| `--skip-incomplete` | n'écrit que les fichiers intégralement récupérés |
| `--no-fill` | tronque au premier trou au lieu de combler de zéros |

Le tri se fait **après** écriture, par déplacement d'un fichier temporaire :
chaque bloc n'est lu qu'une fois. Sonder l'état avant d'écrire aurait doublé le
temps de lecture — inacceptable sur plusieurs téraoctets.

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

## Mémoire : le cache de blocs est borné

Le lecteur DMU garde en mémoire les blocs déjà lus, ce qui évite de relire
cent fois les mêmes blocs indirects. Ce cache doit rester **borné** : le
système live tourne entièrement en RAM, et une extraction parcourt des
centaines de milliers de blocs pouvant peser jusqu'à 1 Mio chacun. Sans limite,
la session finit par être tuée en cours d'extraction.

- plafond par défaut : **256 Mio**, réglable par la variable d'environnement
  `ZFSRESCUE_CACHE` (en octets) ;
- éviction LRU ;
- les blocs volumineux (> plafond/8) ne sont pas conservés : les données de
  fichier ne sont lues qu'une fois, les garder chasserait des métadonnées utiles.

```bash
ZFSRESCUE_CACHE=$((64<<20)) python3 -m zfsrescue extract ...   # machine limitée
```

Si une extraction est interrompue malgré tout, reprenez **dataset par dataset**
avec `--dataset <nom ou numéro d'objet>` : chaque exécution repart à zéro et
n'accumule rien d'une passe à l'autre.

## Récapituler un rapport sans relire les disques

```bash
python3 -m zfsrescue resume /mnt/rescue/07_extraction.json
```

Rend le bilan par dataset et les totaux à partir d'un rapport JSON déjà
produit — utile pour relire un résultat sans rien recalculer, et sans avoir à
taper de commande compliquée sur une console.

## Rien n'interrompt une extraction

Sur de vraies données, les métadonnées reconstituées peuvent se contredire :
un objet en désigne un autre comme répertoire parent alors que celui-ci est un
fichier. Le chemin reconstruit est alors impossible à créer.

Deux garde-fous :

1. **À la reconstruction des chemins** — un parent qui n'est pas un répertoire
   n'est pas suivi : l'objet est rattaché à `(parent perdu)`, et l'anomalie est
   consignée dans ses erreurs. On ne fabrique pas d'arborescence impossible.
2. **À l'écriture** — si le système de fichiers refuse quand même le chemin
   (`ENOTDIR`, `EEXIST`, `EISDIR`, nom trop long…), le fichier est écrit **à
   plat** dans `_conflits/<objet>_<chemin aplati>`, avec la mention
   correspondante dans le rapport. Toute autre erreur est consignée et
   l'extraction **continue** : une anomalie sur un fichier ne doit jamais faire
   perdre les heures de travail déjà effectuées ni les fichiers suivants.

Trois tests couvrent ce comportement, dont celui du cas rencontré en
production : un fichier existant là où il faudrait un répertoire.

## Ranger une extraction déjà faite

Si une extraction a été produite par une version antérieure — ou avec
`--ecrire-perdus` — le rapport JSON permet de la ranger **sans relire les
disques**, ce qui évite de recommencer des heures de lecture :

```bash
python3 -m zfsrescue trier /mnt/rescue/07_extraction.json --dry-run   # aperçu
python3 -m zfsrescue trier /mnt/rescue/07_extraction.json
```

- les fichiers dont aucun bloc n'était lisible sont **supprimés** ;
- les fichiers partiels sont déplacés dans `_partiels/` ;
- les fichiers complets ne bougent pas.

Chaque suppression est vérifiée sur le fichier lui-même : il doit avoir la
taille annoncée **et** ne contenir que des zéros. Au moindre écart, le fichier
est conservé et signalé. Dans le doute, on ne supprime pas.
