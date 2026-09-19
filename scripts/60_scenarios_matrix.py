#!/usr/bin/env python3
"""
Matrice des scenarios : pour chaque paire de disques survivants, mesure ce que
l'outil recupere reellement et verifie qu'il ne se trompe jamais.

Pour chaque paire :
  * extraction (en memoire, sans ecriture) de tous les datasets ;
  * comparaison des sha256 annonces avec les fichiers d'origine ;
  * bilan fichiers / octets.

    python3 scripts/60_scenarios_matrix.py [--json reports/matrice.json]
"""
import argparse
import hashlib
import itertools
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfsrescue.extract import extraire_noeuds, resume
from zfsrescue.pool import PoolOpenError, open_pool
from zfsrescue.zpl import ZplReader, build_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = {c: os.path.join(ROOT, f"testlab/images/disk{c}.img") for c in "abcd"}
REFERENCE = os.path.join(ROOT, "testlab/reference")
LETTRE_COLONNE = {"a": 0, "b": 1, "c": 2, "d": 3}


def sha256(chemin):
    h = hashlib.sha256()
    with open(chemin, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def empreintes_reference() -> dict[str, str]:
    out = {}
    for rep, _s, fichiers in os.walk(REFERENCE):
        for f in fichiers:
            chemin = os.path.join(rep, f)
            out[os.path.relpath(chemin, REFERENCE)] = sha256(chemin)
    return out


def scenario(paire, refs) -> dict:
    images = [IMAGES[c] for c in paire]
    debut = time.time()
    resultat = {"survivants": list(paire),
                "colonnes": [LETTRE_COLONNE[c] for c in paire],
                "datasets": {}, "faux": [], "fichiers": {}, "octets": {}}
    try:
        pool = open_pool(images)
    except PoolOpenError as exc:
        resultat["erreur"] = str(exc)
        return resultat
    try:
        tous = []
        for n in pool.dataset_tree().walk():
            if n.dataset is None or n.dataset.bp is None or n.dataset.bp.is_hole:
                continue
            objset, res = pool.dmu.read_objset(n.dataset.bp)
            if objset is None:
                resultat["datasets"][n.name] = {"lisible": False,
                                                "etat": res.status}
                continue
            zpl = ZplReader(pool.dmu, objset)
            idx = build_index(zpl)
            fichiers = extraire_noeuds(pool.dmu,
                                       sorted(idx.files, key=lambda x: x.path),
                                       None)
            tous += fichiers
            r = resume(fichiers)
            resultat["datasets"][n.name] = {
                "lisible": True, "resume": r,
                "sa_infere": zpl.inferred_layouts,
                "dnodes_illisibles": idx.summary()["dnodes_unreadable"]}

            sous = n.name.split("/", 1)
            if len(sous) == 2:
                for f in fichiers:
                    cle = os.path.join(sous[1], f.path.lstrip("/"))
                    attendu = refs.get(cle)
                    if attendu is None:
                        continue
                    if f.state in ("COMPLET", "VIDE") and f.sha256 != attendu:
                        resultat["faux"].append(f"{n.name}{f.path}")
        total = resume(tous)
        resultat["fichiers"] = total["by_state"]
        resultat["fichiers"]["total"] = total["files_total"]
        resultat["octets"] = {
            "recuperes": total["bytes_recovered"],
            "perdus": total["bytes_missing"],
            "ratio": round(100 * total["ratio_bytes"], 2)}
        resultat["blocs"] = {"total": total["blocks_total"],
                             "ok": total["blocks_ok"],
                             "reconstruits": total["blocks_reconstructed"]}
    finally:
        pool.close()
    resultat["duree_s"] = round(time.time() - debut, 1)
    return resultat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="FICHIER")
    args = ap.parse_args()

    print("Calcul des empreintes de reference...")
    refs = empreintes_reference()
    print(f"  {len(refs)} fichiers d'origine\n")

    resultats = []
    entete = (f"{'survivants':<12}{'colonnes':<11}{'complets':>9}"
              f"{'partiels':>9}{'perdus':>8}{'total':>7}"
              f"{'blocs reconstr.':>17}{'octets %':>10}{'faux':>6}")
    print(entete)
    print("-" * len(entete))
    for paire in itertools.combinations("abcd", 2):
        r = scenario(paire, refs)
        resultats.append(r)
        f = r.get("fichiers", {})
        print(f"{'+'.join(paire):<12}{str(r['colonnes']):<11}"
              f"{f.get('COMPLET', 0):>9}{f.get('PARTIEL', 0):>9}"
              f"{f.get('PERDU', 0):>8}{f.get('total', 0):>7}"
              f"{r.get('blocs', {}).get('reconstruits', 0):>17}"
              f"{r.get('octets', {}).get('ratio', 0):>9.1f}%"
              f"{len(r['faux']):>6}")

    faux = sum(len(r["faux"]) for r in resultats)
    print()
    if faux:
        print(f"ECHEC : {faux} fichier(s) declare(s) recupere(s) mais differents "
              "de l'original")
    else:
        print("Aucun fichier declare recupere n'est faux, dans aucun scenario.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": resultats, "fichiers_reference": len(refs)},
                      fh, indent=2, ensure_ascii=False)
        print(f"Rapport JSON : {args.json}")
    return 1 if faux else 0


if __name__ == "__main__":
    sys.exit(main())
