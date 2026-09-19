"""Interface en ligne de commande de zfsrescue."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .raidz import (analyse_availability, raidz_asize, raidz_map_alloc)
from .uberblock import UberblockSet, guid_sum_expected, read_uberblocks
from .pool import PoolOpenError, open_pool
from .zap import read_zap
from .zpl import ZplReader, build_index
from .extract import extraire_noeuds, resume as resume_extraction
from .readonly import ReadOnlyError
from .report import build_report, render_json, render_text
from .topology import build_topology, scan_devices


def cmd_labels(args: argparse.Namespace) -> int:
    try:
        scanned = scan_devices(args.images)
    except (ReadOnlyError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2

    try:
        hashes = {}
        if args.sha256:
            for s in scanned:
                hashes[s.device.info.path] = s.device.sha256()

        topo = build_topology(scanned)
        report = build_report(scanned, topo,
                              include_full_labels=args.full_labels,
                              hashes=hashes or None)

        if not args.quiet:
            print(render_text(report))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(render_json(report) + "\n")
            if not args.quiet:
                print(f"\nRapport JSON ecrit : {args.json}")
        elif args.json_stdout:
            print(render_json(report))

        if topo.inconsistencies:
            return 1
        return 0
    finally:
        for s in scanned:
            s.device.close()


def _params_from_report(chemin: str) -> dict:
    """Recupere ashift/ncols/nparity et les colonnes disponibles d'un rapport d'etape 2."""
    with open(chemin, encoding="utf-8") as fh:
        rep = json.load(fh)
    tl = rep["pool"]["top_level_vdevs"][0]
    dispo = [c["id"] for c in tl["children"] if c["state"] == "AVAILABLE"]
    return {"ashift": tl["ashift"], "ncols": tl["children_total"],
            "nparity": tl["nparity"], "available": dispo,
            "pool": rep["pool"]["name"]}


def cmd_raidz_map(args: argparse.Namespace) -> int:
    params = {"ashift": args.ashift, "ncols": args.ncols,
              "nparity": args.nparity, "available": None, "pool": None}
    if args.from_report:
        params.update(_params_from_report(args.from_report))
    if args.available is not None:
        params["available"] = [int(x) for x in args.available.split(",") if x != ""]
    for k in ("ashift", "ncols", "nparity"):
        if params[k] is None:
            print(f"erreur : --{k} manquant (ou utilisez --from-report)", file=sys.stderr)
            return 2

    if args.dva:
        champs = args.dva.split(":")
        if len(champs) != 3:
            print("erreur : format DVA attendu vdev:offset:asize (hexadecimal)",
                  file=sys.stderr)
            return 2
        vdev, offset, dva_asize = int(champs[0], 16), int(champs[1], 16), int(champs[2], 16)
    else:
        vdev, offset, dva_asize = 0, args.offset, None

    if args.psize is None:
        print("erreur : --psize est obligatoire (taille physique du bloc)",
              file=sys.stderr)
        return 2

    rm = raidz_map_alloc(offset, args.psize, params["ashift"], params["ncols"],
                         params["nparity"])

    sortie = {"input": {"vdev": vdev, "offset": offset, "psize": args.psize,
                        "dva_asize": dva_asize, **{k: params[k] for k in
                                                   ("ashift", "ncols", "nparity")}},
              "map": rm.as_dict()}

    coherent = None
    if dva_asize is not None:
        coherent = (dva_asize == rm.dva_asize)
        sortie["dva_asize_coherent"] = coherent

    if params["available"] is not None:
        av = analyse_availability(rm, params["available"])
        sortie["availability"] = av.as_dict()
        sortie["available_columns"] = params["available"]

    if args.json_stdout:
        print(json.dumps(sortie, indent=2, ensure_ascii=False))
        return 0

    print(f"Bloc  offset={offset:#x}  psize={args.psize:#x} "
          f"({args.psize} octets)  ashift={params['ashift']} "
          f"ncols={params['ncols']} nparity={params['nparity']}")
    print(f"  colonnes accedees : {rm.acols}   colonnes vues : {rm.scols}   "
          f"colonnes larges : {rm.bigcols}   secteurs de padding : {rm.nskip}")
    print(f"  asize colonnes    : {rm.asize}   asize DVA attendu : {rm.dva_asize} "
          f"({rm.dva_asize:#x})")
    if dva_asize is not None:
        print(f"  asize du DVA lu   : {dva_asize} ({dva_asize:#x}) -> "
              f"{'COHERENT' if coherent else 'INCOHERENT'}")
    print(f"  asize par vdev_raidz_asize() : {raidz_asize(args.psize, params['ashift'], params['ncols'], params['nparity'])}")
    print()
    print("  idx  role     colonne(devidx)  offset allouable     offset physique      taille")
    for c in rm.columns:
        dispo = ""
        if params["available"] is not None:
            dispo = "  [disponible]" if c.devidx in params["available"] else "  [MISSING]"
        print(f"  {c.index:>3}  {c.role:<7}  {c.devidx:>14}  {c.offset:>18}  "
              f"{c.phys_offset:>18}  {c.size:>7}{dispo}")
    if params["available"] is not None:
        av = analyse_availability(rm, params["available"])
        print()
        print(f"  ETAT DU BLOC : {av.status}")
        print(f"    {av.detail}")
    return 0


def cmd_uberblocks(args: argparse.Namespace) -> int:
    """Etape 4 : decouverte et validation des uberblocks."""
    scanned = scan_devices(args.images)
    try:
        topo = build_topology(scanned)
        if not topo.top_levels:
            print("erreur : aucune topologie exploitable (labels illisibles)",
                  file=sys.stderr)
            return 2
        tl = topo.top_levels[0]
        ashift = args.ashift or tl.ashift
        if ashift is None:
            print("erreur : ashift inconnu, utilisez --ashift", file=sys.stderr)
            return 2

        jeu = UberblockSet()
        for sd in scanned:
            jeu.all += read_uberblocks(sd.device, ashift)

        attendu = guid_sum_expected(
            topo.pool_guid or 0, [t.guid or 0 for t in topo.top_levels],
            [c.guid or 0 for t in topo.top_levels for c in t.children])

        meilleur = jeu.best
        avertissements = []
        if meilleur is None:
            avertissements.append("aucun uberblock valide : pool non exploitable")
        else:
            if meilleur.guid_sum != attendu:
                avertissements.append(
                    f"ub_guid_sum ({meilleur.guid_sum}) different de la somme des "
                    f"guid connus ({attendu}) : il existe probablement des vdev "
                    "que les labels disponibles ne decrivent pas")
            if meilleur.raidz_expansion_active:
                avertissements.append(
                    "HORS PERIMETRE: expansion RAIDZ detectee "
                    f"(etat={meilleur.raidz_reflow_state}, "
                    f"offset={meilleur.raidz_reflow_offset}) — le mapping standard "
                    "ne s'applique pas a tout le vdev")
            if meilleur.checkpoint_txg:
                avertissements.append(
                    f"le pool porte un checkpoint (txg {meilleur.checkpoint_txg})")
            if meilleur.mmp_enabled:
                avertissements.append(
                    "MMP actif : le pool etait peut-etre importe ailleurs")

        rapport = {
            "pool": {"name": topo.name, "pool_guid": topo.pool_guid,
                     "txg_from_label": topo.txg},
            "ashift": ashift,
            "ring": {"uberblock_size": 1 << min(max(ashift, 10), 13),
                     "slots_per_label": (128 << 10) >> min(max(ashift, 10), 13),
                     "labels_per_device": 4,
                     "devices": len(scanned)},
            "counts": {"slots_examined": len(jeu.all), "with_magic": len(jeu.present),
                       "valid": len(jeu.valid), "corrupt": len(jeu.corrupt)},
            "guid_sum_expected": attendu,
            "best": meilleur.as_dict() if meilleur else None,
            "history": jeu.history(),
            "warnings": avertissements,
        }
        if args.all:
            rapport["all_uberblocks"] = [u.as_dict() for u in jeu.all if u.magic_ok]

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rapport, indent=2, ensure_ascii=False) + "\n")
        if args.json_stdout:
            print(json.dumps(rapport, indent=2, ensure_ascii=False))
            return 1 if avertissements else 0

        c = rapport["counts"]
        print(f"Pool {topo.name}  (pool_guid={topo.pool_guid})   ashift={ashift}")
        print(f"Anneaux : {rapport['ring']['slots_per_label']} creneaux de "
              f"{rapport['ring']['uberblock_size']} octets x 4 labels x "
              f"{len(scanned)} support(s)")
        print(f"Creneaux examines {c['slots_examined']}, portant un magic "
              f"{c['with_magic']}, valides {c['valid']}, corrompus {c['corrupt']}")
        print()
        if meilleur:
            print("MEILLEUR UBERBLOCK (txg le plus eleve, checksum valide)")
            print(f"  txg            : {meilleur.txg}")
            print(f"  horodatage     : {meilleur.timestamp_iso}")
            print(f"  version        : {meilleur.version}  "
                  f"(logiciel {meilleur.software_version})")
            print(f"  creneau        : {meilleur.slot} "
                  f"(= txg % {rapport['ring']['slots_per_label']})")
            print(f"  somme des guid : {meilleur.guid_sum} "
                  f"{'(coherente)' if meilleur.guid_sum == attendu else '(INCOHERENTE)'}")
            print(f"  checkpoint     : {meilleur.checkpoint_txg}")
            print(f"  expansion RAIDZ: etat={meilleur.raidz_reflow_state} "
                  f"({rapport['best']['raidz_reflow_state_name']})")
            print(f"  MOS (rootbp)   : {meilleur.rootbp}")
            print(f"  copies du MOS  : {meilleur.rootbp.copies}")
            print()
        print("HISTORIQUE DES ETATS EXPLOITABLES")
        print("     txg  horodatage UTC              copies  creneau  MOS")
        for h in jeu.history()[:args.limit]:
            coh = "" if h["coherent_copies"] else "  [COPIES DIVERGENTES]"
            print(f"  {h['txg']:>6}  {h['timestamp_utc']:<28} {h['copies']:>5}  "
                  f"{h['slot']:>7}  {h['rootbp_copies']} copie(s){coh}")
        reste = len(jeu.history()) - args.limit
        if reste > 0:
            print(f"  ... {reste} etat(s) plus ancien(s) (option --limit)")
        if avertissements:
            print()
            print("AVERTISSEMENTS")
            for a in avertissements:
                print(f"  ! {a}")
        if args.json:
            print(f"\nRapport JSON ecrit : {args.json}")
        return 1 if avertissements else 0
    finally:
        for sd in scanned:
            sd.device.close()


def cmd_mos(args: argparse.Namespace) -> int:
    """Etape 5/6 : lecture du MOS et de la hierarchie des datasets."""
    try:
        pool = open_pool(args.images, txg=args.txg)
    except PoolOpenError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    try:
        mos = pool.mos
        meta = mos.meta_dnode

        objets = []
        types = {}
        illisibles = 0
        for oid, dn, etat in pool.dmu.iter_dnodes(meta):
            if dn is None:
                illisibles += 1
                continue
            if not dn.allocated:
                continue
            types[dn.type_name] = types.get(dn.type_name, 0) + 1
            objets.append({"id": oid, **dn.as_dict()})

        annuaire = read_zap(pool.dmu,
                            pool.dmu.read_dnode(meta, 1)[0])
        arbre = pool.dataset_tree()

        rapport = {
            **pool.summary(),
            "mos_meta_dnode": meta.as_dict(),
            "objects_found": len(objets),
            "dnodes_unreadable": illisibles,
            "objects_by_type": dict(sorted(types.items())),
            "object_directory": annuaire.as_dict(),
            "datasets": arbre.as_dict(),
        }
        if args.objects:
            rapport["objects"] = objets

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rapport, indent=2, ensure_ascii=False) + "\n")
        if args.json_stdout:
            print(json.dumps(rapport, indent=2, ensure_ascii=False))
            return 0

        s = pool.summary()
        print(f"Pool {s['pool']}   txg {s['uberblock']['txg']}   "
              f"{s['uberblock']['timestamp_utc']}")
        print(f"Colonnes disponibles {s['vdev']['columns_available']}, "
              f"MANQUANTES {s['vdev']['columns_missing']}")
        print()
        print("MOS")
        print(f"  objets annonces (fill)   : {s['mos']['objects_declared']}")
        print(f"  objets alloues retrouves : {len(objets)}")
        print(f"  dnodes illisibles        : {illisibles}")
        print(f"  tableau des dnodes       : {meta.nblocks} blocs de "
              f"{meta.datablksize} octets, {meta.nlevels} niveau(x)")
        print()
        print("  objets par type :")
        for t, n in sorted(types.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>4}  {t}")
        print()
        print(f"ANNUAIRE D'OBJETS (objet 1, ZAP {annuaire.kind}, "
              f"complet={annuaire.complete})")
        for e in sorted(annuaire.entries, key=lambda e: e.name):
            if len(e.values) == 1:
                print(f"    {e.name:<30} -> {e.values[0]}")
            else:
                print(f"    {e.name:<30} -> {len(e.values)} valeur(s)")
        print()
        print("DATASETS")
        for n in arbre.walk():
            ds = n.dataset
            marque = "" if not n.errors else "  [!] " + " ; ".join(n.errors)
            if ds is None:
                print(f"    {n.name:<24} (pas de dataset){marque}")
                continue
            print(f"    {n.name:<24} obj={ds.object_id:<5} txg={ds.creation_txg:<4} "
                  f"refere={ds.referenced_bytes:>11}  "
                  f"snapshots={[x for x, _ in n.snapshots] or '-'}{marque}")
            print(f"        objset : {ds.bp}")
        if pool.warnings:
            print()
            print("AVERTISSEMENTS")
            for w in pool.warnings:
                print(f"  ! {w}")
        if args.json:
            print(f"\nRapport JSON ecrit : {args.json}")
        return 0
    finally:
        pool.close()


def _datasets_zpl(pool, filtre: str | None):
    """Rend (nom, objset, index) pour chaque dataset ZPL exploitable."""
    for n in pool.dataset_tree().walk():
        if filtre and n.name != filtre:
            continue
        if n.dataset is None or n.dataset.bp is None or n.dataset.bp.is_hole:
            continue
        objset, res = pool.dmu.read_objset(n.dataset.bp)
        if objset is None:
            yield n.name, None, None, res.status
            continue
        zpl = ZplReader(pool.dmu, objset)
        yield n.name, zpl, build_index(zpl), None


def cmd_ls(args: argparse.Namespace) -> int:
    """Etape 6 : arborescence des fichiers recuperables."""
    try:
        pool = open_pool(args.images, txg=args.txg)
    except PoolOpenError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    try:
        rapport = {**pool.summary(), "datasets": []}
        for nom, zpl, idx, echec in _datasets_zpl(pool, args.dataset):
            if idx is None:
                rapport["datasets"].append(
                    {"name": nom, "readable": False, "reason": echec})
                if not args.quiet:
                    print(f"\n=== {nom} : OBJSET ILLISIBLE ({echec}) ===")
                continue
            s = idx.summary()
            entree = {"name": nom, "readable": True, **s,
                      "sa_layout_inferred": zpl.inferred_layouts}
            if args.files:
                entree["objects"] = [n.as_dict() for n in
                                     sorted(idx.nodes.values(),
                                            key=lambda x: x.path)]
            rapport["datasets"].append(entree)

            if args.quiet:
                continue
            print(f"\n=== {nom} ===")
            print(f"  dnodes lus {s['dnodes_total'] - s['dnodes_unreadable']}"
                  f"/{s['dnodes_total']}   objets retrouves {s['objects_found']}"
                  f"   fichiers {s['files']}")
            print(f"  nommes par un repertoire : {s['named_from_directories']}"
                  f"   orphelins : {s['orphans']}"
                  f"   repertoires lus : {s['directories_read']}")
            if zpl.inferred_layouts:
                print(f"  ATTENTION : {zpl.inferred_layouts} jeu(x) d'attributs "
                      "reconstitues par hypothese validee (disposition SA illisible)")
            for e in s["errors"]:
                print(f"  ! {e}")
            noeuds = sorted(idx.nodes.values(), key=lambda x: x.path)
            for n in noeuds[:args.limit]:
                if not (n.is_file or n.is_dir):
                    continue
                marque = "d" if n.is_dir else "-"
                taille = "" if n.size is None else f"{n.size:>12}"
                hyp = " ~" if n.attrs_inferred else "  "
                print(f"   {marque}{hyp}{taille}  {n.path}")
            reste = len([n for n in noeuds if n.is_file or n.is_dir]) - args.limit
            if reste > 0:
                print(f"   ... {reste} entree(s) de plus (--limit)")

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rapport, indent=2, ensure_ascii=False) + "\n")
            if not args.quiet:
                print(f"\nRapport JSON ecrit : {args.json}")
        return 0
    finally:
        pool.close()


def cmd_extract(args: argparse.Namespace) -> int:
    """Etapes 7 et 8 : extraction, y compris partielle."""
    if not args.dry_run and not args.dest:
        print("erreur : --dest est obligatoire (ou utilisez --dry-run)",
              file=sys.stderr)
        return 2
    if args.dest and os.path.abspath(args.dest) in [
            os.path.abspath(os.path.dirname(i)) for i in args.images]:
        print("erreur : la destination ne doit pas etre le repertoire des images",
              file=sys.stderr)
        return 2
    try:
        pool = open_pool(args.images, txg=args.txg)
    except PoolOpenError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    try:
        rapport = {**pool.summary(), "destination": args.dest,
                   "dry_run": bool(args.dry_run), "datasets": []}
        total = []
        for nom, zpl, idx, echec in _datasets_zpl(pool, args.dataset):
            if idx is None:
                rapport["datasets"].append({"name": nom, "readable": False,
                                            "reason": echec})
                print(f"{nom:<24} objset illisible ({echec})")
                continue
            prefixe = nom.replace("/", "_")
            resultats = extraire_noeuds(
                pool.dmu, sorted(idx.files, key=lambda n: n.path),
                None if args.dry_run else args.dest, prefixe=prefixe,
                combler=not args.no_fill,
                sauter_incomplets=args.skip_incomplete)
            total += resultats
            r = resume_extraction(resultats)
            rapport["datasets"].append(
                {"name": nom, "readable": True, "summary": r,
                 "sa_layout_inferred": zpl.inferred_layouts,
                 "files": [x.as_dict(with_blocks=args.blocks)
                           for x in resultats]})
            print(f"{nom:<24} {r['files_total']:>5} fichiers  "
                  + "  ".join(f"{k}={v}" for k, v in r["by_state"].items())
                  + f"   octets {r['bytes_recovered']}/"
                    f"{r['bytes_recovered'] + r['bytes_missing']}"
                    f" ({100 * r['ratio_bytes']:.1f} %)")
        rapport["total"] = resume_extraction(total)
        print()
        print("TOTAL")
        t = rapport["total"]
        print(f"  fichiers        : {t['files_total']}  "
              + "  ".join(f"{k}={v}" for k, v in t["by_state"].items()))
        print(f"  blocs           : {t['blocks_ok']}/{t['blocks_total']} valides"
              f" (dont {t['blocks_reconstructed']} reconstruits par la parite)")
        print(f"  octets de donnees : {t['bytes_recovered']} recuperes, "
              f"{t['bytes_missing']} perdus ({100 * t['ratio_bytes']:.1f} %)")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rapport, indent=2, ensure_ascii=False) + "\n")
            print(f"\nRapport JSON ecrit : {args.json}")
        return 0
    finally:
        pool.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="zfsrescue",
        description="Lecteur forensic ZFS en lecture seule "
                    "(RAIDZ1 4 vdev, 2 absents).")
    ap.add_argument("--version", action="version", version=f"zfsrescue {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("labels",
                       help="lit les labels des supports fournis et reconstruit "
                            "la topologie du pool")
    p.add_argument("images", nargs="+",
                   help="images disque ou peripheriques bloc (lecture seule)")
    p.add_argument("--json", metavar="FICHIER", help="ecrit le rapport JSON")
    p.add_argument("--json-stdout", action="store_true",
                   help="affiche le rapport JSON sur la sortie standard")
    p.add_argument("--full-labels", action="store_true",
                   help="inclut la configuration complete de chaque label dans le JSON")
    p.add_argument("--sha256", action="store_true",
                   help="calcule l'empreinte SHA-256 de chaque support (lecture integrale)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="n'affiche pas le rapport texte")
    p.set_defaults(func=cmd_labels)

    r = sub.add_parser("raidz-map",
                       help="calcule le mapping RAIDZ d'un bloc "
                            "(colonnes, offsets physiques, recuperabilite)")
    r.add_argument("--dva", help="DVA au format vdev:offset:asize (hexadecimal, "
                                 "comme affiche par zdb)")
    r.add_argument("--offset", type=lambda x: int(x, 0),
                   help="offset du bloc dans la zone allouable du vdev")
    r.add_argument("--psize", type=lambda x: int(x, 0),
                   help="taille physique du bloc (octets)")
    r.add_argument("--ashift", type=int)
    r.add_argument("--ncols", type=int, help="nombre de colonnes du vdev raidz")
    r.add_argument("--nparity", type=int)
    r.add_argument("--from-report", metavar="RAPPORT.json",
                   help="reprend ashift/ncols/nparity et les colonnes disponibles "
                        "d'un rapport produit par la commande 'labels'")
    r.add_argument("--available", metavar="0,2,3",
                   help="colonnes (devidx) effectivement disponibles")
    r.add_argument("--json-stdout", action="store_true")
    r.set_defaults(func=cmd_raidz_map)

    u = sub.add_parser("uberblocks",
                       help="lit et valide les uberblocks des supports fournis")
    u.add_argument("images", nargs="+")
    u.add_argument("--ashift", type=int,
                   help="force l'ashift (sinon deduit des labels)")
    u.add_argument("--limit", type=int, default=20,
                   help="nombre d'etats historiques affiches")
    u.add_argument("--all", action="store_true",
                   help="inclut tous les uberblocks dans le JSON")
    u.add_argument("--json", metavar="FICHIER")
    u.add_argument("--json-stdout", action="store_true")
    u.set_defaults(func=cmd_uberblocks)

    m = sub.add_parser("mos",
                       help="lit le MOS et la hierarchie des datasets")
    m.add_argument("images", nargs="+")
    m.add_argument("--txg", type=int,
                   help="utilise l'uberblock de ce txg au lieu du plus recent")
    m.add_argument("--objects", action="store_true",
                   help="detaille chaque objet du MOS dans le JSON")
    m.add_argument("--json", metavar="FICHIER")
    m.add_argument("--json-stdout", action="store_true")
    m.set_defaults(func=cmd_mos)

    l = sub.add_parser("ls", help="liste les fichiers recuperables")
    l.add_argument("images", nargs="+")
    l.add_argument("--dataset", help="limite a un dataset")
    l.add_argument("--txg", type=int)
    l.add_argument("--limit", type=int, default=40)
    l.add_argument("--files", action="store_true",
                   help="detaille chaque objet dans le JSON")
    l.add_argument("--json", metavar="FICHIER")
    l.add_argument("-q", "--quiet", action="store_true")
    l.set_defaults(func=cmd_ls)

    e = sub.add_parser("extract",
                       help="extrait les fichiers vers un autre support")
    e.add_argument("images", nargs="+")
    e.add_argument("--dest", help="repertoire de destination")
    e.add_argument("--dataset", help="limite a un dataset")
    e.add_argument("--txg", type=int)
    e.add_argument("--dry-run", action="store_true",
                   help="analyse sans rien ecrire")
    e.add_argument("--no-fill", action="store_true",
                   help="arrete un fichier au premier bloc manquant au lieu "
                        "de combler avec des zeros")
    e.add_argument("--skip-incomplete", action="store_true",
                   help="n'ecrit que les fichiers integralement recuperes")
    e.add_argument("--blocks", action="store_true",
                   help="detaille l'etat de chaque bloc dans le JSON")
    e.add_argument("--json", metavar="FICHIER")
    e.set_defaults(func=cmd_extract)

    args = ap.parse_args(argv)
    return args.func(args)
