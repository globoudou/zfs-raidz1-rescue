"""
Extraction des fichiers vers un support de destination.

Etapes 7 et 8 du cahier des charges. Chaque bloc porte un etat explicite :

    RECOVERED                      lu et checksum valide
    RECOVERED_WITH_RECONSTRUCTION  reconstruit par la parite, checksum valide
    INVALID_CHECKSUM               donnees obtenues mais checksum faux
    MISSING_DATA                   colonnes absentes : irrecuperable
    UNKNOWN                        cas non gere

Un fichier n'est declare complet que si TOUS ses blocs sont valides. Les blocs
absents sont ecrits comme des zeros (option) mais ne sont JAMAIS comptes comme
recuperes, et le rapport indique precisement leur position.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .blockio import (INVALID_CHECKSUM, MISSING_DATA, RECOVERED,
                      RECOVERED_WITH_RECONSTRUCTION, UNKNOWN)
from .dmu import DmuReader, Dnode
from .zpl import IndexedNode

ETAT_COMPLET = "COMPLET"
ETAT_PARTIEL = "PARTIEL"
ETAT_PERDU = "PERDU"
ETAT_VIDE = "VIDE"

ETATS_OK = (RECOVERED, RECOVERED_WITH_RECONSTRUCTION)


@dataclass
class BlocFichier:
    blkid: int
    status: str
    offset: int
    length: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"blkid": self.blkid, "status": self.status,
                "offset": self.offset, "length": self.length,
                "detail": self.detail}


@dataclass
class ResultatFichier:
    object_id: int
    path: str
    size: int | None
    blocksize: int
    blocks: list[BlocFichier] = field(default_factory=list)
    bytes_recovered: int = 0
    bytes_missing: int = 0
    sha256: str | None = None
    written_to: str | None = None
    errors: list[str] = field(default_factory=list)
    size_inferred: bool = False

    @property
    def state(self) -> str:
        if self.size == 0:
            return ETAT_VIDE
        if not self.blocks:
            return ETAT_PERDU
        bons = sum(1 for b in self.blocks if b.status in ETATS_OK)
        if bons == len(self.blocks):
            return ETAT_COMPLET
        if bons == 0:
            return ETAT_PERDU
        return ETAT_PARTIEL

    @property
    def blocks_ok(self) -> int:
        return sum(1 for b in self.blocks if b.status in ETATS_OK)

    @property
    def blocks_reconstructed(self) -> int:
        return sum(1 for b in self.blocks
                   if b.status == RECOVERED_WITH_RECONSTRUCTION)

    def as_dict(self, with_blocks: bool = False) -> dict[str, Any]:
        d = {
            "object_id": self.object_id, "path": self.path, "size": self.size,
            "size_inferred": self.size_inferred,
            "blocksize": self.blocksize, "state": self.state,
            "blocks_total": len(self.blocks), "blocks_ok": self.blocks_ok,
            "blocks_reconstructed": self.blocks_reconstructed,
            "bytes_recovered": self.bytes_recovered,
            "bytes_missing": self.bytes_missing,
            "sha256": self.sha256, "written_to": self.written_to,
            "errors": self.errors,
        }
        if with_blocks:
            d["blocks"] = [b.as_dict() for b in self.blocks]
        else:
            d["bad_blocks"] = [b.as_dict() for b in self.blocks
                               if b.status not in ETATS_OK]
        return d

    def carte(self, largeur: int = 64) -> str:
        """Carte compacte des blocs : . = ok, r = reconstruit, X = perdu, ? = checksum."""
        symboles = {RECOVERED: ".", RECOVERED_WITH_RECONSTRUCTION: "r",
                    MISSING_DATA: "X", INVALID_CHECKSUM: "?", UNKNOWN: "u"}
        s = "".join(symboles.get(b.status, "u") for b in self.blocks)
        return "\n".join(s[i:i + largeur] for i in range(0, len(s), largeur))


def extraire_fichier(dmu: DmuReader, dn: Dnode, chemin_dest: str | None,
                     taille: int | None, object_id: int, chemin: str,
                     combler: bool = True, size_inferred: bool = False
                     ) -> ResultatFichier:
    """
    Lit tous les blocs d'un fichier et, si `chemin_dest` est fourni, l'ecrit.

    `combler` : ecrire des zeros a la place des blocs absents (extraction
    partielle). Sinon le fichier s'arrete au premier bloc manquant.
    """
    res = ResultatFichier(object_id=object_id, path=chemin, size=taille,
                          blocksize=dn.datablksize, size_inferred=size_inferred)

    if taille == 0 or dn.nblocks == 0:
        # Fichier vide : le contenu est connu et complet, meme en analyse seule.
        res.sha256 = hashlib.sha256(b"").hexdigest()
        if chemin_dest:
            os.makedirs(os.path.dirname(chemin_dest) or ".", exist_ok=True)
            with open(chemin_dest, "wb"):
                pass
            res.written_to = chemin_dest
        return res

    h = hashlib.sha256()
    fh = None
    if chemin_dest:
        os.makedirs(os.path.dirname(chemin_dest) or ".", exist_ok=True)
        fh = open(chemin_dest, "wb")
    try:
        reste = taille
        for blkid in range(dn.nblocks):
            b = dmu.read_object_block(dn, blkid)
            debut = blkid * dn.datablksize
            longueur = dn.datablksize
            if reste is not None:
                longueur = max(0, min(dn.datablksize, reste))
                reste -= longueur
            bloc = BlocFichier(blkid=blkid, status=b.status, offset=debut,
                               length=longueur, detail=b.detail)
            res.blocks.append(bloc)

            if b.status in ETATS_OK and b.data is not None:
                donnees = b.data[:longueur]
                res.bytes_recovered += len(donnees)
            else:
                res.bytes_missing += longueur
                if not combler:
                    res.errors.append(
                        f"extraction interrompue au bloc {blkid} ({b.status})")
                    break
                donnees = b"\x00" * longueur
            h.update(donnees)
            if fh is not None:
                fh.write(donnees)
    finally:
        if fh is not None:
            fh.close()
            res.written_to = chemin_dest
    res.sha256 = h.hexdigest()
    return res


def extraire_noeuds(dmu: DmuReader, noeuds: Iterable[IndexedNode],
                    racine_dest: str | None, prefixe: str = "",
                    combler: bool = True,
                    sauter_incomplets: bool = False) -> list[ResultatFichier]:
    """
    Extrait une liste de fichiers. `racine_dest` a None = analyse seule
    (aucune ecriture nulle part).
    """
    out = []
    for n in noeuds:
        if n.dnode is None or not n.is_file:
            continue
        rel = n.path.lstrip("/") or f"objet_{n.object_id}"
        rel = rel.replace("(parent perdu)", "_parent_perdu")
        dest = None
        if racine_dest:
            dest = os.path.join(racine_dest, prefixe, rel)
            dest = os.path.normpath(dest)
            if not dest.startswith(os.path.normpath(racine_dest)):
                out.append(ResultatFichier(
                    object_id=n.object_id, path=n.path, size=n.size,
                    blocksize=n.dnode.datablksize,
                    errors=["chemin sortant du repertoire de destination : ignore"]))
                continue

        if sauter_incomplets and dest:
            # premiere passe sans ecriture pour connaitre l'etat
            essai = extraire_fichier(dmu, n.dnode, None, n.size, n.object_id,
                                     n.path, combler, n.attrs_inferred)
            if essai.state != ETAT_COMPLET:
                essai.errors.append("fichier incomplet : non ecrit "
                                    "(--skip-incomplete)")
                out.append(essai)
                continue

        out.append(extraire_fichier(dmu, n.dnode, dest, n.size, n.object_id,
                                    n.path, combler, n.attrs_inferred))
    return out


def resume(resultats: list[ResultatFichier]) -> dict[str, Any]:
    par_etat: dict[str, int] = {}
    octets_ok = octets_perdus = 0
    blocs_ok = blocs_total = blocs_reconstruits = 0
    for r in resultats:
        par_etat[r.state] = par_etat.get(r.state, 0) + 1
        octets_ok += r.bytes_recovered
        octets_perdus += r.bytes_missing
        blocs_ok += r.blocks_ok
        blocs_total += len(r.blocks)
        blocs_reconstruits += r.blocks_reconstructed
    return {
        "files_total": len(resultats),
        "by_state": dict(sorted(par_etat.items())),
        "bytes_recovered": octets_ok,
        "bytes_missing": octets_perdus,
        "blocks_total": blocs_total,
        "blocks_ok": blocs_ok,
        "blocks_reconstructed": blocs_reconstruits,
        "ratio_bytes": (octets_ok / (octets_ok + octets_perdus)
                        if (octets_ok + octets_perdus) else 1.0),
    }
