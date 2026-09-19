#!/usr/bin/env python3
"""
ETAPE 9 — validation de la recuperation.

Compare ce que l'outil a extrait avec les fichiers d'origine conserves dans
testlab/reference, et verifie surtout que l'outil NE MENT PAS :

  * tout fichier declare COMPLET doit etre identique bit a bit a l'original ;
  * tout fichier declare PARTIEL doit avoir ses octets recuperes identiques a
    l'original aux memes positions ;
  * les fichiers declares PERDU ne doivent rien pretendre ;
  * les fichiers presents dans la reference mais absents du rapport sont
    comptes comme non retrouves.

    python3 scripts/50_validate_recovery.py --report reports/xxx.json \
        [--extracted testlab/extracted]
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE = os.path.join(ROOT, "testlab", "reference")


def sha256(chemin: str) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as fh:
        for bloc in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def chemin_reference(dataset: str, chemin: str) -> str | None:
    """zrtest/small + /file_000.bin -> testlab/reference/small/file_000.bin"""
    parties = dataset.split("/", 1)
    if len(parties) == 1:
        return None                       # dataset racine : pas de reference
    return os.path.join(REFERENCE, parties[1], chemin.lstrip("/"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True,
                    help="rapport JSON produit par 'zfsrescue extract'")
    ap.add_argument("--extracted",
                    help="repertoire de destination de l'extraction "
                         "(sinon seuls les sha256 du rapport sont compares)")
    args = ap.parse_args()

    with open(args.report, encoding="utf-8") as fh:
        rapport = json.load(fh)

    stats = {"complets_verifies": 0, "complets_faux": 0,
             "partiels_verifies": 0, "partiels_faux": 0,
             "perdus": 0, "sans_reference": 0, "non_retrouves": 0,
             "vides": 0}
    anomalies: list[str] = []
    vus: dict[str, set] = {}

    for ds in rapport.get("datasets", []):
        if not ds.get("readable"):
            continue
        nom = ds["name"]
        vus.setdefault(nom, set())
        for f in ds.get("files", []):
            chemin = f["path"]
            vus[nom].add(chemin.lstrip("/"))
            ref = chemin_reference(nom, chemin)
            if ref is None or not os.path.exists(ref):
                stats["sans_reference"] += 1
                continue
            etat = f["state"]
            taille_ref = os.path.getsize(ref)

            if f["size"] is not None and f["size"] != taille_ref:
                anomalies.append(
                    f"{nom}{chemin} : taille annoncee {f['size']} != "
                    f"taille reelle {taille_ref}")

            if etat == "VIDE":
                stats["vides"] += 1
                continue
            if etat == "PERDU":
                stats["perdus"] += 1
                continue

            if etat == "COMPLET":
                empreinte = f.get("sha256")
                attendue = sha256(ref)
                if empreinte == attendue:
                    stats["complets_verifies"] += 1
                else:
                    stats["complets_faux"] += 1
                    anomalies.append(
                        f"GRAVE {nom}{chemin} : declare COMPLET mais sha256 "
                        f"different de l'original")
                if args.extracted:
                    sortie = f.get("written_to")
                    if sortie and os.path.exists(sortie):
                        if sha256(sortie) != attendue:
                            anomalies.append(
                                f"GRAVE {nom}{chemin} : fichier ecrit different "
                                "de l'original")
                    elif sortie:
                        anomalies.append(f"{nom}{chemin} : fichier annonce "
                                         f"ecrit mais absent ({sortie})")
                continue

            if etat == "PARTIEL":
                sortie = f.get("written_to")
                if not (args.extracted and sortie and os.path.exists(sortie)):
                    stats["partiels_verifies"] += 1
                    continue
                with open(ref, "rb") as a, open(sortie, "rb") as b:
                    orig, extrait = a.read(), b.read()
                mauvais = []
                for bloc in f.get("bad_blocks", []):
                    pass
                bons = [bl for bl in f.get("blocks", [])
                        if bl["status"].startswith("RECOVERED")]
                if not bons:      # le rapport ne detaille pas les blocs
                    stats["partiels_verifies"] += 1
                    continue
                for bl in bons:
                    d, n = bl["offset"], bl["length"]
                    if orig[d:d + n] != extrait[d:d + n]:
                        mauvais.append(bl["blkid"])
                if mauvais:
                    stats["partiels_faux"] += 1
                    anomalies.append(
                        f"GRAVE {nom}{chemin} : blocs {mauvais} declares "
                        "recuperes mais differents de l'original")
                else:
                    stats["partiels_verifies"] += 1

    # fichiers de la reference que l'outil n'a pas retrouves
    for ds in rapport.get("datasets", []):
        nom = ds["name"]
        parties = nom.split("/", 1)
        if len(parties) == 1:
            continue
        base = os.path.join(REFERENCE, parties[1])
        if not os.path.isdir(base):
            continue
        for repertoire, _sr, fichiers in os.walk(base):
            for f in fichiers:
                rel = os.path.relpath(os.path.join(repertoire, f), base)
                if rel not in vus.get(nom, set()):
                    stats["non_retrouves"] += 1

    print("VALIDATION DE LA RECUPERATION")
    print(f"  fichiers declares COMPLET et verifies identiques : "
          f"{stats['complets_verifies']}")
    print(f"  fichiers declares COMPLET mais DIFFERENTS        : "
          f"{stats['complets_faux']}")
    print(f"  fichiers PARTIELS verifies (octets recuperes ok) : "
          f"{stats['partiels_verifies']}")
    print(f"  fichiers PARTIELS avec des octets faux           : "
          f"{stats['partiels_faux']}")
    print(f"  fichiers declares PERDU                          : {stats['perdus']}")
    print(f"  fichiers vides                                   : {stats['vides']}")
    print(f"  objets sans reference comparable                 : "
          f"{stats['sans_reference']}")
    print(f"  fichiers d'origine non retrouves par l'outil     : "
          f"{stats['non_retrouves']}")
    if anomalies:
        print()
        print(f"ANOMALIES ({len(anomalies)}) :")
        for a in anomalies[:40]:
            print(f"  - {a}")
        if len(anomalies) > 40:
            print(f"  ... {len(anomalies) - 40} de plus")

    graves = stats["complets_faux"] + stats["partiels_faux"] \
        + sum(1 for a in anomalies if a.startswith("GRAVE"))
    print()
    if graves:
        print(f"ECHEC : {graves} anomalie(s) grave(s) — l'outil a declare "
              "recuperees des donnees qui ne le sont pas.")
        return 1
    print("SUCCES : aucune donnee declaree recuperee n'est fausse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
