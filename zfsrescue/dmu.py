"""
Couche DMU : objsets, dnodes, et lecture du contenu d'un objet.

Reference normative : OpenZFS 2.3.9
  * objset_phys_t   include/sys/dmu_objset.h:80-95
  * zil_header_t    include/sys/zil.h:63-71
  * dnode_phys_t    include/sys/dnode.h:219-275
  * constantes      include/sys/dnode.h:60-100

objset_phys_t (4096 octets au maximum) :
    0     os_meta_dnode      dnode_phys_t (512 octets) : le tableau des dnodes
    512   os_zil_header      zil_header_t (192 octets)
    704   os_type            dmu_objset_type_t
    712   os_flags
    720   os_portable_mac[32]
    752   os_local_mac[32]
    1024  os_userused_dnode  (a partir de OBJSET_PHYS_SIZE_V2)
    ...

dnode_phys_t (512 octets, ou plus si dn_extra_slots) :
    0   dn_type          1   dn_indblkshift   2  dn_nlevels    3  dn_nblkptr
    4   dn_bonustype     5   dn_checksum      6  dn_compress   7  dn_flags
    8   dn_datablkszsec (u16)                 10 dn_bonuslen (u16)
    12  dn_extra_slots   13-15 pad
    16  dn_maxblkid      24  dn_used          32 dn_pad3[4]
    64  dn_blkptr[dn_nblkptr] puis le bonus, et le spill blkptr en fin de dnode
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import zfs_enums as E
from .blkptr import (BLKPTR_SIZE, BlockPointer, object_type_name,
                     parse_blkptr)
from .blockio import (MISSING_DATA, RECOVERED, RECOVERED_WITH_RECONSTRUCTION,
                      UNKNOWN, BlockResult, PoolReader)

DNODE_SHIFT = 9
DNODE_MIN_SIZE = 1 << DNODE_SHIFT              # 512
DNODE_CORE_SIZE = 64
SPA_BLKPTRSHIFT = 7                             # blkptr_t = 128 octets
ZIL_HEADER_SIZE = 8 + 8 + BLKPTR_SIZE + 8 + 8 + 8 + 24   # 192
OBJSET_PHYS_SIZE_V1 = 1024
OBJSET_PHYS_SIZE_V2 = 2048
OBJSET_PHYS_SIZE_V3 = 4096

DNODE_FLAG_USED_BYTES = 1 << 0
DNODE_FLAG_SPILL_BLKPTR = 1 << 2

DMU_OST_NAMES = {0: "none", 1: "meta (MOS)", 2: "zfs", 3: "zvol", 4: "other",
                 5: "any"}

# Objets « meta-dnode » speciaux du MOS
DMU_META_DNODE_OBJECT = 0
DMU_OBJECT_DIRECTORY_OBJECT = 1


class DmuError(Exception):
    pass


@dataclass
class Dnode:
    object_id: int | None
    type: int
    indblkshift: int
    nlevels: int
    nblkptr: int
    bonustype: int
    checksum: int
    compress: int
    flags: int
    datablkszsec: int
    bonuslen: int
    extra_slots: int
    maxblkid: int
    used: int
    blkptrs: list[BlockPointer] = field(default_factory=list)
    bonus: bytes = b""
    spill: BlockPointer | None = None
    raw: bytes = b""

    @property
    def allocated(self) -> bool:
        return self.type != 0

    @property
    def datablksize(self) -> int:
        return self.datablkszsec * 512

    @property
    def indblksize(self) -> int:
        return 1 << self.indblkshift

    @property
    def blkptrs_per_indirect(self) -> int:
        return self.indblksize >> SPA_BLKPTRSHIFT

    @property
    def epbs(self) -> int:
        """log2 du nombre de blkptr par bloc indirect."""
        return self.indblkshift - SPA_BLKPTRSHIFT

    @property
    def dnode_size(self) -> int:
        return (1 + self.extra_slots) << DNODE_SHIFT

    @property
    def type_name(self) -> str:
        return object_type_name(self.type)

    @property
    def bonustype_name(self) -> str:
        return object_type_name(self.bonustype)

    @property
    def used_bytes(self) -> int:
        """dn_used est en octets ou en secteurs de 512 selon dn_flags."""
        return self.used if self.flags & DNODE_FLAG_USED_BYTES else self.used * 512

    @property
    def nblocks(self) -> int:
        """Nombre de blocs de niveau 0 (dn_maxblkid est le plus grand id)."""
        return 0 if not self.allocated else self.maxblkid + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id, "type": self.type,
            "type_name": self.type_name, "levels": self.nlevels,
            "nblkptr": self.nblkptr, "datablksize": self.datablksize,
            "indblksize": self.indblksize, "maxblkid": self.maxblkid,
            "nblocks": self.nblocks, "used_bytes": self.used_bytes,
            "bonustype": self.bonustype, "bonustype_name": self.bonustype_name,
            "bonuslen": self.bonuslen, "extra_slots": self.extra_slots,
            "flags": self.flags, "has_spill": self.spill is not None,
            "checksum": E.ZIO_CHECKSUM_NAMES.get(self.checksum, self.checksum),
            "compress": E.ZIO_COMPRESS_NAMES.get(self.compress, self.compress),
        }


def parse_dnode(raw: bytes, offset: int = 0, object_id: int | None = None) -> Dnode:
    if len(raw) - offset < DNODE_MIN_SIZE:
        raise DmuError("tampon trop court pour un dnode")
    (dn_type, indblkshift, nlevels, nblkptr, bonustype, cksum, compress,
     flags) = struct.unpack_from("<8B", raw, offset)
    datablkszsec, bonuslen = struct.unpack_from("<2H", raw, offset + 8)
    extra_slots = raw[offset + 12]
    maxblkid, used = struct.unpack_from("<2Q", raw, offset + 16)

    taille = (1 + extra_slots) << DNODE_SHIFT
    dn = Dnode(object_id=object_id, type=dn_type, indblkshift=indblkshift,
               nlevels=nlevels, nblkptr=nblkptr, bonustype=bonustype,
               checksum=cksum, compress=compress, flags=flags,
               datablkszsec=datablkszsec, bonuslen=bonuslen,
               extra_slots=extra_slots, maxblkid=maxblkid, used=used,
               raw=bytes(raw[offset:offset + min(taille, len(raw) - offset)]))

    if dn_type == 0:
        return dn

    base = offset + DNODE_CORE_SIZE
    for i in range(nblkptr):
        dn.blkptrs.append(parse_blkptr(raw, base + i * BLKPTR_SIZE))

    debut_bonus = base + nblkptr * BLKPTR_SIZE
    dn.bonus = bytes(raw[debut_bonus:debut_bonus + bonuslen])

    if flags & DNODE_FLAG_SPILL_BLKPTR:
        fin = offset + taille
        dn.spill = parse_blkptr(raw, fin - BLKPTR_SIZE)
    return dn


@dataclass
class Objset:
    meta_dnode: Dnode
    type: int
    flags: int
    zil_log: BlockPointer | None = None
    raw_size: int = 0

    @property
    def type_name(self) -> str:
        return DMU_OST_NAMES.get(self.type, f"type_{self.type}")

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "type_name": self.type_name,
                "flags": self.flags,
                "meta_dnode": self.meta_dnode.as_dict()}


def parse_objset(raw: bytes) -> Objset:
    if len(raw) < OBJSET_PHYS_SIZE_V1:
        raise DmuError(f"objset trop court : {len(raw)} octets")
    meta = parse_dnode(raw, 0, object_id=DMU_META_DNODE_OBJECT)
    zil_log = parse_blkptr(raw, DNODE_MIN_SIZE + 16)
    os_type, os_flags = struct.unpack_from("<2Q", raw,
                                           DNODE_MIN_SIZE + ZIL_HEADER_SIZE)
    return Objset(meta_dnode=meta, type=os_type, flags=os_flags,
                  zil_log=zil_log, raw_size=len(raw))


# --- lecture du contenu d'un objet -------------------------------------------
@dataclass
class ObjectBlock:
    """Resultat de la lecture d'un bloc de niveau 0 d'un objet."""
    blkid: int
    status: str
    data: bytes | None
    detail: str = ""
    chemin: list[str] = field(default_factory=list)  # chemin indirect parcouru

    def as_dict(self) -> dict[str, Any]:
        return {"blkid": self.blkid, "status": self.status,
                "length": len(self.data) if self.data else 0,
                "detail": self.detail, "chemin": self.chemin}


class DmuReader:
    """
    Lit les objets d'un objset a travers l'arbre de block pointers.
    Toute lecture impossible est signalee, jamais comblee.
    """

    def __init__(self, reader: PoolReader):
        self.reader = reader
        self._cache: dict[tuple, BlockResult] = {}

    def _read_bp(self, bp: BlockPointer) -> BlockResult:
        cle = (bp.raw,)
        res = self._cache.get(cle)
        if res is None:
            res = self.reader.read_block(bp)
            self._cache[cle] = res
        return res

    def read_objset(self, bp: BlockPointer) -> tuple[Objset | None, BlockResult]:
        res = self._read_bp(bp)
        if not res.ok or res.data is None:
            return None, res
        return parse_objset(res.data), res

    # -- descente dans l'arbre indirect ------------------------------------
    def block_pointer_for(self, dn: Dnode, blkid: int
                          ) -> tuple[BlockPointer | None, str, list[str]]:
        """
        Retourne le blkptr de niveau 0 du bloc `blkid`, en descendant l'arbre.
        Le deuxieme element est un etat (RECOVERED / MISSING_DATA / ...) decrivant
        la lecture des blocs indirects traverses.
        """
        chemin: list[str] = []
        if dn.nlevels == 0:
            return None, UNKNOWN, chemin
        if blkid > dn.maxblkid:
            return None, UNKNOWN, chemin

        niveau = dn.nlevels - 1
        index_haut = blkid >> (dn.epbs * niveau) if niveau else blkid
        if index_haut >= dn.nblkptr:
            return None, UNKNOWN, chemin
        bp = dn.blkptrs[index_haut]
        chemin.append(f"L{niveau}[{index_haut}]")

        while niveau > 0:
            if bp.is_hole:
                return bp, RECOVERED, chemin       # trou : bloc entierement nul
            res = self._read_bp(bp)
            if not res.ok or res.data is None:
                return None, res.status, chemin
            niveau -= 1
            index = (blkid >> (dn.epbs * niveau)) & ((1 << dn.epbs) - 1)
            if (index + 1) * BLKPTR_SIZE > len(res.data):
                return None, UNKNOWN, chemin
            bp = parse_blkptr(res.data, index * BLKPTR_SIZE)
            chemin.append(f"L{niveau}[{index}]")
        return bp, RECOVERED, chemin

    def read_object_block(self, dn: Dnode, blkid: int) -> ObjectBlock:
        bp, etat, chemin = self.block_pointer_for(dn, blkid)
        if bp is None:
            return ObjectBlock(blkid, etat, None, chemin=chemin,
                               detail="bloc indirect illisible ou id hors bornes")
        if bp.is_hole:
            return ObjectBlock(blkid, RECOVERED, b"\x00" * dn.datablksize,
                               chemin=chemin, detail="trou")
        res = self._read_bp(bp)
        data = res.data
        if res.ok and data is not None and len(data) < dn.datablksize:
            data = data + b"\x00" * (dn.datablksize - len(data))
        return ObjectBlock(blkid, res.status, data if res.ok else None,
                           chemin=chemin, detail=res.detail)

    def read_object(self, dn: Dnode, size: int | None = None,
                    max_blocks: int | None = None
                    ) -> tuple[bytes, list[ObjectBlock]]:
        """
        Lit tout l'objet. Les blocs manquants sont remplaces par des zeros
        DANS LE TAMPON RENDU, mais leur etat reel figure dans la liste des
        blocs : l'appelant doit la consulter avant de considerer la donnee
        comme fidele.
        """
        blocs: list[ObjectBlock] = []
        morceaux: list[bytes] = []
        n = dn.nblocks if max_blocks is None else min(dn.nblocks, max_blocks)
        for blkid in range(n):
            b = self.read_object_block(dn, blkid)
            blocs.append(b)
            morceaux.append(b.data if b.data is not None
                            else b"\x00" * dn.datablksize)
        data = b"".join(morceaux)
        if size is not None:
            data = data[:size]
        return data, blocs

    # -- tableau des dnodes -------------------------------------------------
    def dnodes_per_block(self, meta: Dnode) -> int:
        return meta.datablksize // DNODE_MIN_SIZE

    def read_dnode(self, meta: Dnode, object_id: int) -> tuple[Dnode | None, str]:
        """Lit le dnode numero `object_id` dans le tableau des dnodes de l'objset."""
        par_bloc = self.dnodes_per_block(meta)
        blkid = object_id // par_bloc
        index = object_id % par_bloc
        b = self.read_object_block(meta, blkid)
        if b.data is None:
            return None, b.status
        dn = parse_dnode(b.data, index * DNODE_MIN_SIZE, object_id=object_id)
        return dn, b.status

    def iter_dnodes(self, meta: Dnode) -> Iterator[tuple[int, Dnode | None, str]]:
        """Parcourt tous les dnodes de l'objset, bloc par bloc."""
        par_bloc = self.dnodes_per_block(meta)
        for blkid in range(meta.nblocks):
            b = self.read_object_block(meta, blkid)
            base = blkid * par_bloc
            if b.data is None:
                for i in range(par_bloc):
                    yield base + i, None, b.status
                continue
            i = 0
            while i < par_bloc:
                dn = parse_dnode(b.data, i * DNODE_MIN_SIZE,
                                 object_id=base + i)
                yield base + i, dn, b.status
                i += 1 + dn.extra_slots
