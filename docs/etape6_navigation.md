# Étape 6 — Navigation : datasets, répertoires, fichiers

```bash
python3 -m zfsrescue ls testlab/scenarios/missing_a_b/disk?.img --json rapport.json
python3 -m zfsrescue ls ... --dataset zrtest/docs --limit 100
```

## Chemin parcouru

```
dsl_dataset.ds_bp  →  objset du dataset (type ZFS)
   objet 1  « nœud maître »  (ZAP)
       VERSION, ROOT, SA_ATTRS, DELETE_QUEUE, casesensitivity…
   ROOT     →  répertoire racine (ZAP : nom → type<<60 | objet)
   SA_ATTRS →  REGISTRY (nom d'attribut → numéro/longueur)
               LAYOUTS  (numéro → liste ordonnée d'attributs)
   dnode d'un fichier : bonus au format System Attributes
```

Les métadonnées d'un fichier moderne (taille, mode, dates, uid/gid, parent)
sont dans les **System Attributes** du bonus, dont la disposition se lit dans
le dataset lui-même (`sa_impl.h:69-92, 162-186`).

## Deux modes de reconstruction

### 1. Par les répertoires (nominal)

On descend depuis `ROOT`. Chaque répertoire est un ZAP ; une entrée vaut
`type<<60 | numéro d'objet` (`zfs_znode.h:158`).

### 2. Par balayage des dnodes (quand les répertoires manquent)

`build_index()` énumère **tous** les dnodes du dataset, puis lit tous les
répertoires encore lisibles pour retrouver les noms, et reconstruit les chemins
tant que la chaîne de parents tient. Ce qui reste sans parent est conservé et
signalé comme tel (`/(parent perdu)/…`) plutôt qu'écarté.

La racine d'un dataset est **son propre parent** (`zfs_znode.c`) : quand le
nœud maître est perdu, l'outil déduit la racine de cette propriété et le
signale (`racine deduite de l'objet N`).

## Reconstitution de la disposition SA

Si le registre SA du dataset est illisible (ses blocs sont sur les disques
perdus), les tailles et modes seraient inconnus. L'outil tente alors une
**hypothèse validée** :

1. il prend la numérotation standard des attributs ZPL (`zfs_sa.c:48-72`) ;
2. il essaie les dispositions que compose `zfs_mknode()` ;
3. il n'en retient une que si **une seule** passe tous les contrôles :
   - les attributs remplissent **exactement** le bonus (à l'octet près) ;
   - `ZPL_MODE` est un type de fichier valide et **cohérent avec le type du dnode** ;
   - `ZPL_LINKS` non nul et plausible ;
   - `ZPL_SIZE` ≤ espace réellement alloué par le dnode ;
   - `ZPL_MTIME` / `ZPL_CRTIME` dans une plage de dates plausible ;
4. le résultat est **marqué comme hypothèse** (`~` dans la sortie,
   `attrs_inferred: true` dans le JSON, compteur `sa_layout_inferred`).

Si zéro ou plusieurs hypothèses passent, l'outil refuse de choisir.

**Validation** : un test compare, objet par objet, les attributs inférés
(2 disques) avec les attributs réellement lus (4 disques) — `ZPL_SIZE`,
`ZPL_MODE`, `ZPL_LINKS`, `ZPL_UID`, `ZPL_GID`, `ZPL_PARENT`, `ZPL_MTIME`,
`ZPL_CRTIME` doivent être identiques. Sur le pool de test : **215 fichiers,
215 tailles et chemins exacts**.

## Résultats

Colonnes survivantes 2 et 3 (paire alignée) :

| Dataset | objset | nœud maître | fichiers listés | dnodes illisibles |
|---|---|---|---|---|
| `zrtest/docs` | lisible | lisible | 47 | 0 |
| `zrtest/small` | lisible | **perdu** (racine déduite) | 168 sur 200 | 32 |
| `zrtest` | lisible | perdu | 0 | 64 |
| `zrtest/big`, `zrtest/blobs` | lisible | perdu | 0 | 160 (tout le tableau) |
| `zrtest/edge` | **illisible** | — | — | — |

Colonnes survivantes 0 et 2 (paire décalée) : **tous** les datasets, tous les
nœuds maîtres et tous les dnodes sont lisibles — 257 fichiers listés, aucune
hypothèse SA nécessaire.

## Limitations connues

- Les attributs étendus (xattr) et les ACL ne sont pas restitués.
- Les liens symboliques sont listés mais leur cible (attribut `ZPL_SYMLINK`)
  n'est pas encore exploitée.
- La file `DELETE_QUEUE` (fichiers supprimés non libérés) n'est pas parcourue.
- Les snapshots sont listés mais leur contenu n'est pas encore monté ; il est
  accessible en ouvrant leur objset (même code) — à exposer dans la CLI.
