"""
Lecture des ZAP (ZFS Attribute Processor) : les « annuaires » de ZFS.

Reference normative : OpenZFS 2.3.9
  * micro-ZAP        include/sys/zap_impl.h:44-68
  * fat ZAP en-tete  include/sys/zap_impl.h:118-143
  * feuilles         include/sys/zap_leaf.h:40-160
  * lecture valeurs  module/zfs/zap_leaf.c:313-370  (entiers en BIG endian)

Strategie forensic : plutot que de suivre la table de pointeurs (dont un bloc
peut manquer), on ENUMERE tous les blocs de l'objet et on decode chaque bloc
qui se declare comme une feuille. Un bloc illisible retire des entrees mais
n'empeche pas de lire les autres : la liste est alors marquee incomplete.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .blockio import RECOVERED, RECOVERED_WITH_RECONSTRUCTION
from .dmu import Dnode, DmuReader

ZBT_LEAF = (1 << 63) + 0
ZBT_HEADER = (1 << 63) + 1
ZBT_MICRO = (1 << 63) + 3

ZAP_MAGIC = 0x2F52AB2AB
ZAP_LEAF_MAGIC = 0x2AB1EAF

MZAP_ENT_LEN = 64
MZAP_NAME_LEN = MZAP_ENT_LEN - 8 - 4 - 2      # 50
MZAP_HEADER_LEN = 64                           # 8 mots de 64 bits

ZAP_LEAF_CHUNKSIZE = 24
ZAP_LEAF_ARRAY_BYTES = ZAP_LEAF_CHUNKSIZE - 3  # 21
ZAP_LEAF_HEADER_SIZE = 2 * ZAP_LEAF_CHUNKSIZE  # 48
CHAIN_END = 0xFFFF

ZAP_CHUNK_FREE = 253
ZAP_CHUNK_ENTRY = 252
ZAP_CHUNK_ARRAY = 251


class ZapError(Exception):
    pass


@dataclass
class ZapEntry:
    name: str
    int_size: int
    num_ints: int
    values: list[int]
    raw_value: bytes = b""
    cd: int = 0
    source_block: int = 0

    @property
    def value(self) -> int | None:
        return self.values[0] if len(self.values) == 1 else None

    def as_dict(self) -> dict[str, Any]:
        d = {"name": self.name, "int_size": self.int_size,
             "num_ints": self.num_ints, "source_block": self.source_block}
        if len(self.values) == 1:
            d["value"] = self.values[0]
        elif self.int_size == 1:
            d["value_bytes"] = self.raw_value.hex()
        else:
            d["values"] = self.values
        return d


@dataclass
class Zap:
    kind: str                       # "micro" | "fat" | "inconnu"
    entries: list[ZapEntry] = field(default_factory=list)
    declared_entries: int | None = None
    num_leafs: int | None = None
    blocks_total: int = 0
    blocks_read: int = 0
    blocks_missing: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        if self.blocks_missing or self.errors:
            return False
        if self.declared_entries is None:
            return True
        return len(self.entries) == self.declared_entries

    def to_dict(self) -> dict[str, Any]:
        return {e.name: (e.values[0] if len(e.values) == 1 else e.values)
                for e in self.entries}

    def get(self, name: str, default=None):
        for e in self.entries:
            if e.name == name:
                return e.values[0] if len(e.values) == 1 else e.values
        return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "complete": self.complete,
            "entries_found": len(self.entries),
            "entries_declared": self.declared_entries,
            "num_leafs": self.num_leafs,
            "blocks_total": self.blocks_total, "blocks_read": self.blocks_read,
            "blocks_missing": self.blocks_missing, "errors": self.errors,
            "entries": [e.as_dict() for e in self.entries],
        }


def _parse_micro(data: bytes, bloc: int = 0) -> list[ZapEntry]:
    entries = []
    n = (len(data) - MZAP_HEADER_LEN) // MZAP_ENT_LEN
    for i in range(n):
        off = MZAP_HEADER_LEN + i * MZAP_ENT_LEN
        valeur, cd = struct.unpack_from("<QI", data, off)
        nom_brut = data[off + 14:off + 14 + MZAP_NAME_LEN]
        nom = nom_brut.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
        if not nom:
            continue
        entries.append(ZapEntry(name=nom, int_size=8, num_ints=1,
                                values=[valeur], cd=cd, source_block=bloc))
    return entries


def _leaf_geometry(block_size: int) -> tuple[int, int]:
    """(nombre d'entrees de la table de hachage, nombre de chunks)."""
    bs = block_size.bit_length() - 1
    nhash = 1 << (bs - 5)
    nchunks = (block_size - 2 * nhash) // ZAP_LEAF_CHUNKSIZE - 2
    return nhash, nchunks


def _array_bytes(data: bytes, chunk_base: int, nchunks: int, premier: int,
                 nb_octets: int) -> bytes:
    """Suit la chaine de chunks 'array' et rend nb_octets octets."""
    out = bytearray()
    chunk = premier
    garde = 0
    while chunk != CHAIN_END and len(out) < nb_octets:
        if chunk >= nchunks:
            raise ZapError(f"chunk {chunk} hors du bloc")
        off = chunk_base + chunk * ZAP_LEAF_CHUNKSIZE
        if data[off] != ZAP_CHUNK_ARRAY:
            raise ZapError(f"chunk {chunk} n'est pas un tableau")
        out += data[off + 1:off + 1 + ZAP_LEAF_ARRAY_BYTES]
        chunk = struct.unpack_from("<H", data, off + 1 + ZAP_LEAF_ARRAY_BYTES)[0]
        garde += 1
        if garde > nchunks:
            raise ZapError("boucle dans la chaine de chunks")
    return bytes(out[:nb_octets])


def _parse_leaf(data: bytes, bloc: int) -> tuple[list[ZapEntry], list[str]]:
    erreurs: list[str] = []
    entries: list[ZapEntry] = []
    nhash, nchunks = _leaf_geometry(len(data))
    chunk_base = ZAP_LEAF_HEADER_SIZE + 2 * nhash

    magic, nfree, nentries, prefix_len = struct.unpack_from("<IHHH", data, 24)
    if magic != ZAP_LEAF_MAGIC:
        return [], [f"bloc {bloc} : magic de feuille invalide ({magic:#x})"]

    for idx in range(nchunks):
        off = chunk_base + idx * ZAP_LEAF_CHUNKSIZE
        if data[off] != ZAP_CHUNK_ENTRY:
            continue
        (_t, value_intlen, _next, name_chunk, name_numints, value_chunk,
         value_numints, cd) = struct.unpack_from("<BBHHHHHI", data, off)
        try:
            nom_brut = _array_bytes(data, chunk_base, nchunks, name_chunk,
                                    name_numints)
            val_brut = _array_bytes(data, chunk_base, nchunks, value_chunk,
                                    value_intlen * value_numints)
        except ZapError as exc:
            erreurs.append(f"bloc {bloc}, chunk {idx} : {exc}")
            continue
        nom = nom_brut.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
        valeurs = []
        if value_intlen in (1, 2, 4, 8):
            for i in range(value_numints):
                mot = val_brut[i * value_intlen:(i + 1) * value_intlen]
                if len(mot) == value_intlen:
                    valeurs.append(int.from_bytes(mot, "big"))   # zap_leaf.c:355
        entries.append(ZapEntry(name=nom, int_size=value_intlen,
                                num_ints=value_numints, values=valeurs,
                                raw_value=val_brut, cd=cd, source_block=bloc))
    return entries, erreurs


def read_zap(dmu: DmuReader, dn: Dnode, max_blocks: int | None = None) -> Zap:
    """Enumere toutes les entrees d'un objet ZAP."""
    zap = Zap(kind="inconnu")
    if dn.nblocks == 0:
        zap.errors.append("objet vide")
        return zap

    premier = dmu.read_object_block(dn, 0)
    zap.blocks_total = dn.nblocks
    if premier.data is None:
        zap.blocks_missing.append(0)
        zap.errors.append(f"bloc d'en-tete illisible ({premier.status})")
        return zap
    zap.blocks_read += 1

    type_bloc, = struct.unpack_from("<Q", premier.data, 0)
    if type_bloc == ZBT_MICRO:
        zap.kind = "micro"
        zap.entries = _parse_micro(premier.data)
        return zap
    if type_bloc != ZBT_HEADER:
        zap.errors.append(f"type de bloc ZAP inconnu : {type_bloc:#x}")
        return zap

    zap.kind = "fat"
    magic, = struct.unpack_from("<Q", premier.data, 8)
    if magic != ZAP_MAGIC:
        zap.errors.append(f"magic ZAP invalide : {magic:#x}")
        return zap
    # zap_phys_t : block_type(0) magic(8) ptrtbl(16..55) freeblk(56)
    #              num_leafs(64) num_entries(72) salt(80) normflags(88) flags(96)
    zap.num_leafs = struct.unpack_from("<Q", premier.data, 64)[0]
    zap.declared_entries = struct.unpack_from("<Q", premier.data, 72)[0]

    n = dn.nblocks if max_blocks is None else min(dn.nblocks, max_blocks)
    for blkid in range(1, n):
        b = dmu.read_object_block(dn, blkid)
        if b.data is None:
            zap.blocks_missing.append(blkid)
            continue
        zap.blocks_read += 1
        if len(b.data) < ZAP_LEAF_HEADER_SIZE:
            continue
        t, = struct.unpack_from("<Q", b.data, 0)
        if t != ZBT_LEAF:
            continue                      # bloc de table de pointeurs ou libre
        entrees, erreurs = _parse_leaf(b.data, blkid)
        zap.entries += entrees
        zap.errors += erreurs

    vus = set()
    uniques = []
    for e in zap.entries:                 # une feuille peut etre pointee 2 fois
        cle = (e.name, e.cd)
        if cle in vus:
            continue
        vus.add(cle)
        uniques.append(e)
    zap.entries = uniques
    return zap
