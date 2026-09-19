#!/usr/bin/env python3
"""
Anonymise les rapports et la documentation avant publication.

Remplace les elements propres a la machine de developpement :
  * nom d'hote reel        -> testhost
  * hostid reel            -> 0
  * chemins du compte reel -> /srv/zfsrescue

Les GUID du pool de TEST sont conserves : ils ne designent qu'un pool jetable
cree pour les besoins des tests, et la documentation s'y refere.

    python3 scripts/80_anonymiser.py            # applique
    python3 scripts/80_anonymiser.py --check    # verifie qu'il ne reste rien
"""
import argparse
import os
import re
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REMPLACEMENTS = [
    (re.compile(re.escape(socket.gethostname()), re.I), "testhost"),
    (re.compile(r"/home/[a-z_][a-z0-9_-]*/zfsrescue"), "/srv/zfsrescue"),
    (re.compile(r"/home/[a-z_][a-z0-9_-]*"), "/srv"),
    # repertoires de travail temporaires de l'agent (ils portent le nom du compte)
    (re.compile(r"/tmp/[a-z]+-\d+/-home-[a-z0-9_-]+/[0-9a-f-]+/scratchpad"),
     "/tmp/travail"),
    (re.compile(r'("hostid":\s*)\d+'), r"\g<1>0"),
    (re.compile(r"(hostid=)\d+"), r"\g<1>0"),
    (re.compile(r"(hostid\s+:\s+)\d+"), r"\g<1>0"),
]

DOSSIERS = ("reports", "docs", "tests", "scripts", "zfsrescue")
FICHIERS = ("README.md",)
EXTENSIONS = (".json", ".md", ".txt", ".py", ".sh")


def fichiers():
    for nom in FICHIERS:
        chemin = os.path.join(ROOT, nom)
        if os.path.exists(chemin):
            yield chemin
    for d in DOSSIERS:
        base = os.path.join(ROOT, d)
        for rep, _s, noms in os.walk(base):
            if "__pycache__" in rep:
                continue
            for n in noms:
                if n.endswith(EXTENSIONS) and n != os.path.basename(__file__):
                    yield os.path.join(rep, n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    modifies, restants = [], []
    for chemin in fichiers():
        with open(chemin, encoding="utf-8", errors="surrogateescape") as fh:
            avant = fh.read()
        apres = avant
        for motif, remplacement in REMPLACEMENTS:
            apres = motif.sub(remplacement, apres)
        if apres != avant:
            if args.check:
                restants.append(os.path.relpath(chemin, ROOT))
            else:
                with open(chemin, "w", encoding="utf-8",
                          errors="surrogateescape") as fh:
                    fh.write(apres)
                modifies.append(os.path.relpath(chemin, ROOT))

    if args.check:
        if restants:
            print("Elements identifiants encore presents dans :")
            for f in restants:
                print(f"  {f}")
            return 1
        print("Aucun element identifiant detecte.")
        return 0
    print(f"{len(modifies)} fichier(s) anonymise(s)")
    for f in modifies:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
