"""
Lecture d'un bloc ZFS a partir de son block pointer.

Enchaine : DVA -> mapping RAIDZ -> lecture des colonnes -> reconstruction
eventuelle par la parite -> verification du checksum -> decompression.

Etats possibles d'un bloc (imposes par le cahier des charges) :

    RECOVERED                      lu tel quel, checksum valide
    RECOVERED_WITH_RECONSTRUCTION  une colonne reconstruite par la parite,
                                   checksum valide
    INVALID_CHECKSUM               donnees obtenues mais checksum faux
    MISSING_DATA                   colonnes manquantes : irrecuperable
    UNKNOWN                        cas non gere (gang, compression absente...)

Aucune donnee n'est jamais inventee : une colonne absente reste absente.
Un bloc n'est declare valide que si son checksum le confirme.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import checksum as cks
from . import compress as cmp
from .blkptr import DVA, BlockPointer
from .raidz import (ROLE_DATA, ROLE_PARITY, RaidzMap, io_size_for_psize,
                    raidz_map_alloc)
from .readonly import ReadOnlyDevice

RECOVERED = "RECOVERED"
RECOVERED_WITH_RECONSTRUCTION = "RECOVERED_WITH_RECONSTRUCTION"
INVALID_CHECKSUM = "INVALID_CHECKSUM"
MISSING_DATA = "MISSING_DATA"
UNKNOWN = "UNKNOWN"


class BlockReadError(Exception):
    pass


@dataclass
class DvaAttempt:
    dva: str
    status: str
    reconstructed_columns: list[int] = field(default_factory=list)
    missing_columns: list[int] = field(default_factory=list)
    checksum_ok: bool | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"dva": self.dva, "status": self.status,
                "reconstructed_columns": self.reconstructed_columns,
                "missing_columns": self.missing_columns,
                "checksum_ok": self.checksum_ok, "detail": self.detail}


@dataclass
class BlockResult:
    status: str
    data: bytes | None = None            # donnees logiques (lsize octets)
    physical: bytes | None = None        # donnees physiques (psize octets)
    dva_index: int | None = None
    reconstructed: bool = False
    checksum_ok: bool | None = None
    computed_cksum: tuple | None = None
    detail: str = ""
    attempts: list[DvaAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (RECOVERED, RECOVERED_WITH_RECONSTRUCTION)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "dva_index": self.dva_index,
            "reconstructed": self.reconstructed,
            "checksum_ok": self.checksum_ok,
            "length": len(self.data) if self.data is not None else 0,
            "detail": self.detail,
            "attempts": [a.as_dict() for a in self.attempts],
        }


def reconstruct_raidz1(rm: RaidzMap, colonnes: dict[int, bytes | None],
                       manquante: int) -> bytes:
    """
    Reconstruit une colonne de donnees par la parite P (XOR).

    module/zfs/vdev_raidz.c:1114 (vdev_raidz_generate_parity_p) : P vaut le XOR
    de toutes les colonnes de donnees, chacune sur sa propre longueur. Une
    colonne de longueur L se retrouve donc en faisant le XOR de P[0:L] avec les
    L premiers octets de toutes les autres colonnes de donnees.
    """
    parite = rm.columns[0]
    p = colonnes.get(parite.index)
    if p is None:
        raise BlockReadError("colonne de parite absente : reconstruction impossible")
    cible = rm.columns[manquante]
    acc = bytearray(p[:cible.size])
    for c in rm.data_columns:
        if c.index == manquante:
            continue
        d = colonnes.get(c.index)
        if d is None:
            raise BlockReadError(
                f"colonne {c.index} absente : reconstruction impossible")
        for i in range(min(len(d), len(acc))):
            acc[i] ^= d[i]
    return bytes(acc)


class PoolReader:
    """
    Contexte de lecture : les supports disponibles et la geometrie du vdev.
    Les colonnes absentes sont simplement absentes (jamais des zeros).
    """

    def __init__(self, devices: dict[int, ReadOnlyDevice], ashift: int,
                 ncols: int, nparity: int):
        self.devices = devices
        self.ashift = ashift
        self.ncols = ncols
        self.nparity = nparity

    # -- niveau DVA ---------------------------------------------------------
    def read_dva_physical(self, dva: DVA, psize: int) -> tuple[bytes | None, DvaAttempt]:
        """Lit les psize octets physiques d'une copie, avec reconstruction si besoin."""
        att = DvaAttempt(dva=str(dva), status=UNKNOWN)
        if dva.gang:
            att.detail = "bloc gang : non gere a ce stade"
            return None, att

        taille_io = io_size_for_psize(psize, self.ashift)
        rm = raidz_map_alloc(dva.offset, taille_io, self.ashift, self.ncols,
                             self.nparity)
        colonnes: dict[int, bytes | None] = {}
        for col in rm.columns:
            if col.size == 0:
                colonnes[col.index] = b""
                continue
            dev = self.devices.get(col.devidx)
            if dev is None:
                colonnes[col.index] = None
                continue
            try:
                colonnes[col.index] = dev.pread(col.phys_offset, col.size)
            except Exception as exc:                 # secteur illisible
                colonnes[col.index] = None
                att.detail = f"lecture impossible colonne {col.index} : {exc}"

        manquantes_donnees = [c.index for c in rm.data_columns
                              if colonnes.get(c.index) is None]
        manquantes_parite = [c.index for c in rm.parity_columns
                             if colonnes.get(c.index) is None]
        att.missing_columns = sorted(manquantes_donnees + manquantes_parite)

        if manquantes_donnees:
            parite_dispo = len(rm.parity_columns) - len(manquantes_parite)
            if len(manquantes_donnees) > parite_dispo or self.nparity != 1:
                att.status = MISSING_DATA
                att.detail = (f"{len(manquantes_donnees)} colonne(s) de donnees "
                              f"absente(s) pour {parite_dispo} parite(s)")
                return None, att
            try:
                idx = manquantes_donnees[0]
                colonnes[idx] = reconstruct_raidz1(rm, colonnes, idx)
                att.reconstructed_columns = [idx]
            except BlockReadError as exc:
                att.status = MISSING_DATA
                att.detail = str(exc)
                return None, att

        morceaux = []
        for c in rm.data_columns:
            d = colonnes[c.index]
            if d is None or len(d) != c.size:
                att.status = MISSING_DATA
                att.detail = f"colonne {c.index} incomplete"
                return None, att
            morceaux.append(d)
        brut = b"".join(morceaux)
        att.status = (RECOVERED_WITH_RECONSTRUCTION if att.reconstructed_columns
                      else RECOVERED)
        return brut[:psize], att

    # -- niveau bloc --------------------------------------------------------
    def read_block(self, bp: BlockPointer) -> BlockResult:
        """Lit et valide le bloc designe par `bp`, en essayant chaque copie."""
        if bp.is_hole:
            return BlockResult(status=RECOVERED, data=b"\x00" * bp.lsize,
                               physical=b"", checksum_ok=None,
                               detail="trou : zeros legitimes")
        if bp.embedded:
            charge = bp.embedded_payload()
            try:
                data = cmp.decompress(bp.compress, charge, bp.lsize)
            except cmp.DecompressionError as exc:
                return BlockResult(status=UNKNOWN, detail=str(exc))
            return BlockResult(status=RECOVERED, data=data, physical=charge,
                               checksum_ok=None,
                               detail="donnees embarquees dans le block pointer")
        if bp.uses_crypt:
            return BlockResult(status=UNKNOWN,
                               detail="bloc chiffre : hors perimetre")
        if bp.is_gang:
            return BlockResult(status=UNKNOWN,
                               detail="bloc gang : non gere a ce stade")

        resultat = BlockResult(status=MISSING_DATA,
                               detail="aucune copie exploitable")
        byteswap = bp.byteorder != cks.ZFS_HOST_BYTEORDER

        for i, dva in enumerate(bp.dvas):
            if dva.is_empty:
                continue
            brut, att = self.read_dva_physical(dva, bp.psize)
            resultat.attempts.append(att)
            if brut is None:
                continue
            try:
                conforme, calcule = cks.verify(bp.checksum, brut, bp.cksum,
                                               byteswap)
            except cks.ChecksumNonSupporte as exc:
                att.detail = str(exc)
                att.status = UNKNOWN
                resultat.status = UNKNOWN
                resultat.detail = str(exc)
                continue
            att.checksum_ok = conforme
            if not conforme:
                att.status = INVALID_CHECKSUM
                if resultat.status not in (RECOVERED,
                                           RECOVERED_WITH_RECONSTRUCTION):
                    resultat.status = INVALID_CHECKSUM
                    resultat.detail = ("checksum non conforme : la copie est "
                                       "corrompue ou mal reconstruite")
                    resultat.computed_cksum = calcule
                continue

            try:
                data = cmp.decompress(bp.compress, brut, bp.lsize)
            except cmp.CompressionNonSupportee as exc:
                resultat.status = UNKNOWN
                resultat.detail = str(exc)
                att.status = UNKNOWN
                att.detail = str(exc)
                continue
            except cmp.DecompressionError as exc:
                resultat.status = INVALID_CHECKSUM
                resultat.detail = (f"checksum valide mais decompression "
                                   f"impossible : {exc}")
                att.detail = str(exc)
                continue

            return BlockResult(
                status=att.status, data=data, physical=brut, dva_index=i,
                reconstructed=bool(att.reconstructed_columns),
                checksum_ok=True, computed_cksum=calcule,
                detail=("colonne reconstruite par la parite"
                        if att.reconstructed_columns else ""),
                attempts=resultat.attempts)

        if all(a.status == MISSING_DATA for a in resultat.attempts) and resultat.attempts:
            resultat.status = MISSING_DATA
            resultat.detail = ("toutes les copies ont des colonnes manquantes : "
                               "bloc irrecuperable")
        return resultat
