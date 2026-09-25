#!/usr/bin/env python3
"""
Empreinte de controle d'un support, en LECTURE SEULE.

Trois modes, du plus rapide au plus complet :

  echantillon  (defaut)  lit 64 zones de 1 Mio reparties sur tout le support,
                         plus les quatre zones de label ZFS : 64 Mio lus,
                         quelques secondes, et toute ecriture dans une de ces
                         zones est detectee ;
  complete               sha256 integral : la preuve definitive, mais il faut
                         relire tout le support (des heures sur 2 To) ;
  aucune                 ne calcule rien, se contente de l'etat du drapeau
                         « lecture seule » du noyau.

Le controle le plus parlant reste l'etat read-only du peripherique, affiche
dans tous les cas : si le noyau refuse les ecritures, il n'y a rien a detecter.

    python3 scripts/91_empreinte.py --mode echantillon /dev/sda2 /dev/sdc2
"""
import argparse
import hashlib
import os
import stat
import sys

ZONES = 64
TAILLE_ZONE = 1 << 20
TAILLE_LABEL = 256 * 1024


def taille_support(chemin: str) -> int:
    st = os.stat(chemin)
    if stat.S_ISBLK(st.st_mode):
        fd = os.open(chemin, os.O_RDONLY)
        try:
            return os.lseek(fd, 0, os.SEEK_END)
        finally:
            os.close(fd)
    return st.st_size


def lecture_seule(chemin: str) -> str:
    """Etat du drapeau read-only du noyau, pour un peripherique bloc."""
    st = os.stat(chemin)
    if not stat.S_ISBLK(st.st_mode):
        return "fichier"
    nom = os.path.basename(os.path.realpath(chemin))
    for cand in (f"/sys/class/block/{nom}/ro", f"/sys/block/{nom}/ro"):
        try:
            with open(cand) as fh:
                return "BLOQUEE" if fh.read().strip() == "1" else "autorisee"
        except OSError:
            continue
    return "inconnue"


def zones_echantillon(taille: int) -> list[tuple[int, int]]:
    """Zones lues : les quatre labels ZFS, puis un quadrillage regulier."""
    psize = taille - (taille % TAILLE_LABEL)
    zones = [(0, TAILLE_LABEL), (TAILLE_LABEL, TAILLE_LABEL),
             (psize - 2 * TAILLE_LABEL, TAILLE_LABEL),
             (psize - TAILLE_LABEL, TAILLE_LABEL)]
    utile = max(taille - TAILLE_ZONE, 1)
    for i in range(ZONES):
        debut = (utile * i) // max(ZONES - 1, 1)
        debut -= debut % 4096
        zones.append((debut, min(TAILLE_ZONE, taille - debut)))
    vues, propres = set(), []
    for debut, longueur in sorted(zones):
        if debut in vues or longueur <= 0 or debut + longueur > taille:
            continue
        vues.add(debut)
        propres.append((debut, longueur))
    return propres


def empreinte(chemin: str, mode: str) -> tuple[str, int, str]:
    """Retourne (empreinte, octets lus, mode reellement applique)."""
    taille = taille_support(chemin)
    if mode == "echantillon":
        prevu = sum(longueur for _d, longueur in zones_echantillon(taille))
        if prevu >= taille // 2:
            # sur un petit support, echantillonner ne fait rien gagner
            mode = "complete"
    h = hashlib.sha256()
    lus = 0
    drapeaux = os.O_RDONLY | getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(chemin, drapeaux)
    except PermissionError:
        fd = os.open(chemin, os.O_RDONLY)
    try:
        if mode == "complete":
            off = 0
            while off < taille:
                bloc = os.pread(fd, min(8 << 20, taille - off), off)
                if not bloc:
                    break
                h.update(bloc)
                off += len(bloc)
                lus += len(bloc)
        else:
            for debut, longueur in zones_echantillon(taille):
                h.update(debut.to_bytes(8, "little"))
                reste = longueur
                pos = debut
                while reste:
                    bloc = os.pread(fd, min(1 << 20, reste), pos)
                    if not bloc:
                        break
                    h.update(bloc)
                    pos += len(bloc)
                    reste -= len(bloc)
                    lus += len(bloc)
    finally:
        os.close(fd)
    return h.hexdigest(), lus, mode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("aucune", "echantillon", "complete"),
                    default="echantillon")
    ap.add_argument("supports", nargs="+")
    args = ap.parse_args()

    for chemin in args.supports:
        etat = lecture_seule(chemin)
        taille = taille_support(chemin)
        if args.mode == "aucune":
            print(f"-  aucune  ecriture={etat}  {taille}  {chemin}")
            continue
        h, lus, applique = empreinte(chemin, args.mode)
        print(f"{h}  {applique}  ecriture={etat}  {taille}  "
              f"lus={lus}  {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
