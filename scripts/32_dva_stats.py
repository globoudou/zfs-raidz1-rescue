#!/usr/bin/env python3
"""
Statistiques de recuperabilite sur les VRAIS blocs du pool.

Prend les block pointers listes par `zdb -ddddd` (verite terrain), applique
notre mapping RAIDZ, et determine pour chaque bloc s'il reste lisible avec un
sous-ensemble de colonnes.

Un bloc ayant plusieurs copies (ditto blocks des metadonnees) est considere
recuperable des qu'UNE de ses copies l'est.

    python3 scripts/32_dva_stats.py --available 2,3
"""
import argparse
import collections
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfsrescue.raidz import (STATUS_COMPLETE, STATUS_MISSING_DATA,
                             STATUS_RECONSTRUCTIBLE, analyse_availability,
                             io_size_for_psize, raidz_map_alloc)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "testlab/images")
ZDB = shutil.which("zdb") or "/usr/sbin/zdb"

# Un DVA s'ecrit vdev:offset:asize. Les gardes evitent de confondre avec les
# quatre groupes de 16 chiffres hexadecimaux d'un cksum.
RE_DVA = re.compile(r"(?<![0-9a-fA-F:])(\d{1,3}):([0-9a-f]{1,12}):([0-9a-f]{1,8})(?![0-9a-fA-F:])")
RE_TAILLE = re.compile(r"\b([0-9a-f]+)L/([0-9a-f]+)P\b")
RE_NIVEAU = re.compile(r"\bL(\d)\b")


def blocs_du_dataset(dataset: str):
    """Extrait (niveau, psize, [dva...]) de la sortie de zdb -ddddd."""
    out = subprocess.run([ZDB, "-e", "-p", IMAGES_DIR, "-ddddd", dataset],
                         capture_output=True, text=True).stdout
    for ligne in out.splitlines():
        m_taille = RE_TAILLE.search(ligne)
        if not m_taille:
            continue
        if "EMBEDDED" in ligne:          # donnees dans le pointeur : pas de DVA
            continue
        psize = int(m_taille.group(2), 16)
        dvas = [(int(v), int(o, 16), int(a, 16))
                for v, o, a in RE_DVA.findall(ligne)]
        dvas = [d for d in dvas if d[2] > 0]      # ecarte les trous
        if not dvas or psize == 0:
            continue
        m_niv = RE_NIVEAU.search(ligne)
        niveau = int(m_niv.group(1)) if m_niv else 0
        yield niveau, psize, dvas


def etat_du_bloc(psize, dvas, args, dispo):
    """Etat d'un bloc : la meilleure de ses copies decide."""
    taille_io = io_size_for_psize(psize, args.ashift)
    etats = []
    for _vdev, offset, _asize in dvas:
        rm = raidz_map_alloc(offset, taille_io, args.ashift, args.ncols,
                             args.nparity)
        etats.append(analyse_availability(rm, dispo).status)
    if STATUS_COMPLETE in etats:
        return STATUS_COMPLETE
    if STATUS_RECONSTRUCTIBLE in etats:
        return STATUS_RECONSTRUCTIBLE
    return STATUS_MISSING_DATA


def statuts(args, dispo):
    """
    Parcourt tous les blocs du pool et compte les etats.
    Retourne (blocs, blocs L0, octets L0) — les octets ne concernent que les
    donnees de fichiers (niveau 0), qui sont ce qui compte pour l'extraction.
    """
    blocs = collections.Counter()
    blocs_l0 = collections.Counter()
    octets_l0 = collections.Counter()
    for ds in args.datasets.split(","):
        for niveau, psize, dvas in blocs_du_dataset(ds):
            etat = etat_du_bloc(psize, dvas, args, dispo)
            blocs[etat] += 1
            if niveau == 0:
                blocs_l0[etat] += 1
                octets_l0[etat] += psize
    return blocs, blocs_l0, octets_l0


def comparer_toutes_les_paires(args) -> int:
    """Quelle paire de disques survivants permet de recuperer quoi ?"""
    import itertools as it
    print(f"Comparaison des {args.ncols} colonnes prises deux a deux "
          f"(raidz{args.nparity}, ashift={args.ashift})\n")
    print("  survivants   total blocs   complet  reconstruct.     perdu   lisible  "
          "octets L0")
    for paire in it.combinations(range(args.ncols), 2):
        c, _l0, oct_l0 = statuts(args, list(paire))
        tot = sum(c.values())
        lis = c[STATUS_COMPLETE] + c[STATUS_RECONSTRUCTIBLE]
        tot_o = sum(oct_l0.values())
        lis_o = oct_l0[STATUS_COMPLETE] + oct_l0[STATUS_RECONSTRUCTIBLE]
        print(f"  {str(list(paire)):<12} {tot:>11}   {c[STATUS_COMPLETE]:>7}  "
              f"{c[STATUS_RECONSTRUCTIBLE]:>12}  {c[STATUS_MISSING_DATA]:>8}   "
              f"{100 * lis / tot:5.1f} %  {100 * lis_o / tot_o:7.1f} %")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--available", default="2,3",
                    help="colonnes (devidx) disponibles, ex. 2,3")
    ap.add_argument("--datasets", default="zrtest,zrtest/small,zrtest/docs,"
                                          "zrtest/blobs,zrtest/big,zrtest/edge")
    ap.add_argument("--ashift", type=int, default=12)
    ap.add_argument("--ncols", type=int, default=4)
    ap.add_argument("--nparity", type=int, default=1)
    ap.add_argument("--compare-all-pairs", action="store_true",
                    help="compare les 6 scenarios de deux colonnes survivantes")
    args = ap.parse_args()

    if args.compare_all_pairs:
        return comparer_toutes_les_paires(args)

    dispo = [int(x) for x in args.available.split(",") if x != ""]
    par_niveau = collections.defaultdict(collections.Counter)
    par_taille = collections.defaultdict(collections.Counter)
    incoherences = 0
    total = 0

    for ds in args.datasets.split(","):
        for niveau, psize, dvas in blocs_du_dataset(ds):
            total += 1
            etats = []
            for _vdev, offset, dva_asize in dvas:
                taille_io = io_size_for_psize(psize, args.ashift)
                rm = raidz_map_alloc(offset, taille_io, args.ashift,
                                     args.ncols, args.nparity)
                if rm.dva_asize != dva_asize:
                    incoherences += 1
                etats.append(analyse_availability(rm, dispo).status)
            # la meilleure copie decide
            if STATUS_COMPLETE in etats:
                etat = STATUS_COMPLETE
            elif STATUS_RECONSTRUCTIBLE in etats:
                etat = STATUS_RECONSTRUCTIBLE
            else:
                etat = STATUS_MISSING_DATA
            cle = "donnees (L0)" if niveau == 0 else f"metadonnees (L{niveau})"
            par_niveau[cle][etat] += 1
            par_taille[psize][etat] += 1

    def ligne(nom, c):
        tot = sum(c.values())
        lisible = c[STATUS_COMPLETE] + c[STATUS_RECONSTRUCTIBLE]
        return (f"  {nom:<22} {tot:>6}  complet {c[STATUS_COMPLETE]:>6}  "
                f"reconstruct. {c[STATUS_RECONSTRUCTIBLE]:>6}  "
                f"perdu {c[STATUS_MISSING_DATA]:>6}   "
                f"lisible {100 * lisible / tot:5.1f} %")

    print(f"Colonnes disponibles : {dispo}  (sur {args.ncols}, parite {args.nparity})")
    print(f"Blocs analyses : {total}")
    if incoherences:
        print(f"  ATTENTION : {incoherences} bloc(s) dont l'asize du DVA ne "
              "correspond pas a notre calcul")
    else:
        print("  asize de tous les DVA conformes a notre mapping")
    print("\nPar niveau :")
    for nom in sorted(par_niveau, reverse=True):
        print(ligne(nom, par_niveau[nom]))
    print("\nPar taille physique de bloc :")
    for psize in sorted(par_taille):
        print(ligne(f"{psize:>7} o", par_taille[psize]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
