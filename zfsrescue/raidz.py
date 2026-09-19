"""
Mapping RAIDZ : bloc logique -> colonnes -> offsets physiques sur chaque feuille.

Portage fidele de `vdev_raidz_map_alloc()` — module/zfs/vdev_raidz.c:585
d'OpenZFS 2.3.9. Les noms de variables du source (b, s, f, o, q, r, bc, tot,
acols, scols) sont conserves pour permettre une relecture ligne a ligne.

Principe du format :

  b = offset >> ashift          secteur de depart, dans le vdev RAIDZ
  s = taille >> ashift          nombre de secteurs de donnees
  f = b % dcols                 premiere colonne utilisee par ce bloc
  o = (b / dcols) << ashift     offset de depart sur chaque enfant

  q = s / (dcols - nparity)     secteurs de donnees par colonne "normale"
  r = s - q * (dcols - nparity) reste
  bc = r ? r + nparity : 0      colonnes "larges" (q+1 secteurs)
  tot = s + nparity * (q + (r ? 1 : 0))     total secteurs donnees + parite

Les `nparity` premieres colonnes de la rangee portent la parite, les suivantes
les donnees, dans l'ordre. L'offset physique sur une feuille vaut
rc_offset + VDEV_LABEL_START_SIZE (zio.c:1684, zio_vdev_child_io).

Limite assumee : ce module implemente le chemin standard, pas
`vdev_raidz_map_alloc_expanded()` (pools ayant subi une expansion RAIDZ).
L'etat de reflow se lit dans l'uberblock (etape 4) ; il est verifie ailleurs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import consts

ROLE_PARITY = "parity"
ROLE_DATA = "data"
ROLE_SKIP = "skip"

STATUS_COMPLETE = "COMPLETE"                    # toutes les colonnes de donnees lues
STATUS_RECONSTRUCTIBLE = "RECONSTRUCTIBLE"      # pertes <= parite disponible
STATUS_MISSING_DATA = "MISSING_DATA"            # irrecuperable avec ces supports


def _roundup(x: int, y: int) -> int:
    return ((x + y - 1) // y) * y


@dataclass(frozen=True)
class RaidzColumn:
    """Une colonne de la rangee RAIDZ (rc_* de raidz_col_t)."""
    index: int          # position dans la rangee (0..scols-1)
    devidx: int         # numero de l'enfant du vdev qui porte cette colonne
    offset: int         # offset dans la zone allouable de l'enfant
    size: int           # octets (0 pour une colonne sautee)
    role: str

    @property
    def phys_offset(self) -> int:
        """Offset absolu sur le support (zio.c:1684)."""
        return self.offset + consts.VDEV_LABEL_START_SIZE

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "devidx": self.devidx, "role": self.role,
            "offset": self.offset, "phys_offset": self.phys_offset,
            "size": self.size,
        }


@dataclass
class RaidzMap:
    io_offset: int
    io_size: int
    ashift: int
    dcols: int
    nparity: int
    firstdatacol: int
    acols: int
    scols: int
    bigcols: int
    nskip: int
    skipstart: int
    asize: int                       # somme des colonnes accedees (tot << ashift)
    columns: list[RaidzColumn] = field(default_factory=list)

    @property
    def parity_columns(self) -> list[RaidzColumn]:
        return [c for c in self.columns if c.role == ROLE_PARITY]

    @property
    def data_columns(self) -> list[RaidzColumn]:
        return [c for c in self.columns if c.role == ROLE_DATA]

    @property
    def dva_asize(self) -> int:
        """Taille telle qu'elle apparait dans le DVA : colonnes + secteurs de padding."""
        return self.asize + (self.nskip << self.ashift)

    def devidx_used(self) -> set[int]:
        return {c.devidx for c in self.columns if c.size > 0}

    def as_dict(self) -> dict[str, Any]:
        return {
            "io_offset": self.io_offset, "io_size": self.io_size,
            "ashift": self.ashift, "dcols": self.dcols, "nparity": self.nparity,
            "firstdatacol": self.firstdatacol, "acols": self.acols,
            "scols": self.scols, "bigcols": self.bigcols,
            "nskip": self.nskip, "skipstart": self.skipstart,
            "asize": self.asize, "dva_asize": self.dva_asize,
            "columns": [c.as_dict() for c in self.columns],
        }


def raidz_map_alloc(offset: int, size: int, ashift: int, dcols: int,
                    nparity: int) -> RaidzMap:
    """
    module/zfs/vdev_raidz.c:585 — vdev_raidz_map_alloc().

    `offset` : offset du bloc dans la zone allouable du vdev RAIDZ (celui du DVA).
    `size`   : taille physique du bloc (psize), multiple de 2^ashift.
    """
    if dcols <= nparity:
        raise ValueError("dcols doit etre superieur a nparity")
    if nparity < 1:
        raise ValueError("nparity doit valoir au moins 1")
    if offset < 0 or size <= 0:
        raise ValueError("offset/taille invalides")
    if offset & ((1 << ashift) - 1):
        raise ValueError(f"offset {offset} non aligne sur 2^{ashift}")
    if size & ((1 << ashift) - 1):
        raise ValueError(f"taille {size} non alignee sur 2^{ashift}")

    b = offset >> ashift
    s = size >> ashift
    f = b % dcols
    o = (b // dcols) << ashift

    q = s // (dcols - nparity)
    r = s - q * (dcols - nparity)
    bc = 0 if r == 0 else r + nparity
    tot = s + nparity * (q + (0 if r == 0 else 1))

    if q == 0:
        acols = bc
        scols = min(dcols, _roundup(bc, nparity + 1))
    else:
        acols = dcols
        scols = dcols
    assert acols <= scols

    columns: list[RaidzColumn] = []
    asize = 0
    for c in range(scols):
        col = f + c
        coff = o
        if col >= dcols:
            col -= dcols
            coff += 1 << ashift
        if c >= acols:
            csize = 0
        elif c < bc:
            csize = (q + 1) << ashift
        else:
            csize = q << ashift
        role = ROLE_SKIP if csize == 0 else (
            ROLE_PARITY if c < nparity else ROLE_DATA)
        columns.append(RaidzColumn(index=c, devidx=col, offset=coff,
                                   size=csize, role=role))
        asize += csize

    assert asize == tot << ashift, "invariant asize == tot << ashift"
    nskip = _roundup(tot, nparity + 1) - tot
    skipstart = bc

    rm = RaidzMap(io_offset=offset, io_size=size, ashift=ashift, dcols=dcols,
                  nparity=nparity, firstdatacol=nparity, acols=acols,
                  scols=scols, bigcols=bc, nskip=nskip, skipstart=skipstart,
                  asize=asize, columns=columns)

    assert acols >= 2, "au moins une colonne de donnees et une de parite"
    assert columns[0].size == columns[1].size

    # Rotation de parite tous les 1 Mio, imposee par le format pour raidz1
    # (vdev_raidz.c:695 : "implicit on-disk format requirement ... for all
    # eternity, but only for single-parity RAID-Z").
    if rm.firstdatacol == 1 and (offset & (1 << 20)):
        c0, c1 = columns[0], columns[1]
        columns[0] = RaidzColumn(index=0, devidx=c1.devidx, offset=c1.offset,
                                 size=c0.size, role=c0.role)
        columns[1] = RaidzColumn(index=1, devidx=c0.devidx, offset=c0.offset,
                                 size=c1.size, role=c1.role)
        if rm.skipstart == 0:
            rm.skipstart = 1

    return rm


def io_size_for_psize(psize: int, ashift: int) -> int:
    """
    Taille d'E/S reellement transmise au vdev pour un bloc de taille physique
    `psize` : module/zfs/zio.c:4460-4472, zio_vdev_io_start() arrondit toute
    E/S non alignee au secteur du vdev (P2ROUNDUP sur 2^ashift).

    Un bloc dont le psize vaut 1024 octets sur un vdev ashift=12 occupe donc un
    secteur entier de 4096 octets ; seuls les `psize` premiers octets portent
    des donnees.
    """
    if psize <= 0:
        raise ValueError("psize doit etre strictement positif")
    return _roundup(psize, 1 << ashift)


def raidz_asize(psize: int, ashift: int, dcols: int, nparity: int) -> int:
    """module/zfs/vdev_raidz.c:2249 — vdev_raidz_asize(). Taille reservee dans le DVA."""
    asize = ((psize - 1) >> ashift) + 1
    asize += nparity * ((asize + dcols - nparity - 1) // (dcols - nparity))
    return _roundup(asize, nparity + 1) << ashift


# --- disponibilite / recuperabilite ------------------------------------------
@dataclass
class Availability:
    status: str
    missing_data_columns: list[int]      # index dans la rangee
    missing_parity_columns: list[int]
    usable_parity: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "missing_data_columns": self.missing_data_columns,
            "missing_parity_columns": self.missing_parity_columns,
            "usable_parity": self.usable_parity,
            "detail": self.detail,
        }


def analyse_availability(rm: RaidzMap, available_devidx: Iterable[int]) -> Availability:
    """
    Determine, sans rien deviner, si le bloc est lisible avec les supports fournis.

    Regle : une colonne de donnees manquante n'est reconstructible que si le
    nombre de colonnes manquantes (donnees) est inferieur ou egal au nombre de
    colonnes de parite REELLEMENT disponibles pour ce bloc.
    """
    dispo = set(available_devidx)
    md = [c.index for c in rm.data_columns if c.devidx not in dispo]
    mp = [c.index for c in rm.parity_columns if c.devidx not in dispo]
    usable_parity = len(rm.parity_columns) - len(mp)

    if not md:
        return Availability(STATUS_COMPLETE, md, mp, usable_parity,
                            "toutes les colonnes de donnees sont lisibles")
    if len(md) <= usable_parity:
        return Availability(
            STATUS_RECONSTRUCTIBLE, md, mp, usable_parity,
            f"{len(md)} colonne(s) de donnees absente(s) pour {usable_parity} "
            "colonne(s) de parite disponible(s)")
    return Availability(
        STATUS_MISSING_DATA, md, mp, usable_parity,
        f"{len(md)} colonne(s) de donnees absente(s) pour seulement "
        f"{usable_parity} colonne(s) de parite : bloc IRRECUPERABLE")


# --- lecture des colonnes -----------------------------------------------------
def read_columns(rm: RaidzMap, devices: dict[int, Any]) -> dict[int, bytes | None]:
    """
    Lit chaque colonne sur le support correspondant.
    `devices` : devidx -> ReadOnlyDevice. Une colonne dont le support est absent
    vaut None : jamais de zeros, jamais de donnee inventee.
    """
    out: dict[int, bytes | None] = {}
    for col in rm.columns:
        if col.size == 0:
            out[col.index] = b""
            continue
        dev = devices.get(col.devidx)
        out[col.index] = None if dev is None else dev.pread(col.phys_offset, col.size)
    return out


def assemble_data(rm: RaidzMap, column_data: dict[int, bytes | None]) -> bytes | None:
    """
    Concatene les colonnes de donnees dans l'ordre logique
    (vdev_raidz.c:557, vdev_raidz_map_alloc_read : off avance colonne par colonne).
    Retourne None si une colonne de donnees manque.
    """
    morceaux = []
    for col in rm.data_columns:
        d = column_data.get(col.index)
        if d is None:
            return None
        if len(d) != col.size:
            raise ValueError(
                f"colonne {col.index} : {len(d)} octets lus, {col.size} attendus")
        morceaux.append(d)
    data = b"".join(morceaux)
    assert len(data) == rm.io_size, "la concatenation doit rendre exactement psize"
    return data
