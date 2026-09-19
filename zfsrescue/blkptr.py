"""
Decodage des block pointers ZFS (blkptr_t, 128 octets).

Reference normative : include/sys/spa.h d'OpenZFS 2.3.9
  * struct blkptr      spa.h:373
  * macros d'acces     spa.h:391-520
  * blkptr embarque    spa.h:264-356
  * BF64_GET / _SB     include/sys/bitops.h:45-68

Disposition (16 mots de 64 bits, little-endian sur x86) :

    0-5   blk_dva[3]        3 x (dva_word[0], dva_word[1])
    6     blk_prop          taille, compression, type, niveau, checksum...
    7-8   blk_pad[2]
    9-10  blk_birth_word[2] [0] = naissance physique, [1] = logique
    11    blk_fill
    12-15 blk_cksum         checksum 256 bits

Un blkptr « embarque » (bit E de blk_prop) ne pointe nulle part : les donnees
sont stockees dans le blkptr lui-meme.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from . import zfs_enums as E

BLKPTR_SIZE = 128

BP_EMBEDDED_TYPE_DATA = 0
BP_EMBEDDED_TYPE_RESERVED = 1
BP_EMBEDDED_TYPE_REDACTED = 2

# Mots du blkptr qui portent la charge utile d'un bloc embarque
# (spa.h:269-292 : tous sauf le mot 6 (blk_prop) et le mot 10 (birth logique))
BPE_PAYLOAD_WORDS = [0, 1, 2, 3, 4, 5, 7, 8, 9, 11, 12, 13, 14, 15]


DMU_BSWAP_NAMES = {0: "uint8", 1: "uint16", 2: "uint32", 3: "uint64",
                   4: "zap", 5: "dnode", 6: "objset", 7: "znode",
                   8: "oldacl", 9: "acl"}


def object_type_name(t: int) -> str:
    """
    Nom lisible d'un type d'objet DMU.

    Les types >= 0x80 sont les « nouveaux » types (include/sys/dmu.h) :
        bit 0x80 = DMU_OT_NEWTYPE, 0x40 = metadonnee, 0x20 = chiffre,
        bits 0x1f = fonction de byteswap, qui indique la nature du contenu.
    """
    if t & E.DMU_OT_NEWTYPE:
        bswap = t & E.DMU_OT_BYTESWAP_MASK
        parties = [DMU_BSWAP_NAMES.get(bswap, f"bswap{bswap}")]
        if t & E.DMU_OT_METADATA:
            parties.append("metadata")
        if t & E.DMU_OT_ENCRYPTED:
            parties.append("encrypted")
        return "otn:" + ",".join(parties)
    return E.DMU_OBJECT_TYPE_NAMES.get(t, f"type_{t}")


def _bf64_get(x: int, low: int, length: int) -> int:
    """bitops.h:46 — BF64_DECODE."""
    return (x >> low) & ((1 << length) - 1)


def _bf64_get_sb(x: int, low: int, length: int, shift: int, bias: int) -> int:
    """bitops.h:67 — BF64_GET_SB."""
    return (_bf64_get(x, low, length) + bias) << shift


@dataclass(frozen=True)
class DVA:
    """Data Virtual Address : ou se trouve une copie du bloc."""
    word0: int
    word1: int

    @property
    def asize(self) -> int:
        return _bf64_get_sb(self.word0, 0, E.SPA_ASIZEBITS, E.SPA_MINBLOCKSHIFT, 0)

    @property
    def vdev(self) -> int:
        return _bf64_get(self.word0, 32, E.SPA_VDEVBITS)

    @property
    def offset(self) -> int:
        return _bf64_get_sb(self.word1, 0, 63, E.SPA_MINBLOCKSHIFT, 0)

    @property
    def gang(self) -> bool:
        return bool(_bf64_get(self.word1, 63, 1))

    @property
    def is_empty(self) -> bool:
        return self.word0 == 0 and self.word1 == 0

    def __str__(self) -> str:
        return f"{self.vdev}:{self.offset:x}:{self.asize:x}"

    def as_dict(self) -> dict[str, Any]:
        return {"vdev": self.vdev, "offset": self.offset, "asize": self.asize,
                "gang": self.gang, "text": str(self)}


@dataclass
class BlockPointer:
    raw: bytes
    dvas: list[DVA] = field(default_factory=list)
    prop: int = 0
    pad: tuple[int, int] = (0, 0)
    birth_physical: int = 0
    birth_logical: int = 0
    fill_word: int = 0
    cksum: tuple[int, int, int, int] = (0, 0, 0, 0)

    # --- champs derives de blk_prop ---------------------------------------
    @property
    def embedded(self) -> bool:
        return bool(_bf64_get(self.prop, 39, 1))

    @property
    def lsize(self) -> int:
        if self.embedded:
            return (_bf64_get_sb(self.prop, 0, 25, 0, 1)
                    if self.embedded_type == BP_EMBEDDED_TYPE_DATA else 0)
        return _bf64_get_sb(self.prop, 0, E.SPA_LSIZEBITS, E.SPA_MINBLOCKSHIFT, 1)

    @property
    def psize(self) -> int:
        if self.embedded:
            return 0
        return _bf64_get_sb(self.prop, 16, E.SPA_PSIZEBITS, E.SPA_MINBLOCKSHIFT, 1)

    @property
    def embedded_psize(self) -> int:
        """Taille compressee de la charge utile embarquee (spa.h:340)."""
        return _bf64_get_sb(self.prop, 25, 7, 0, 1) if self.embedded else 0

    @property
    def embedded_type(self) -> int:
        return _bf64_get(self.prop, 40, 8) if self.embedded else -1

    @property
    def compress(self) -> int:
        return _bf64_get(self.prop, 32, E.SPA_COMPRESSBITS)

    @property
    def checksum(self) -> int:
        return 2 if self.embedded else _bf64_get(self.prop, 40, 8)  # OFF si embarque

    @property
    def type(self) -> int:
        return _bf64_get(self.prop, 48, 8)

    @property
    def level(self) -> int:
        return _bf64_get(self.prop, 56, 5)

    @property
    def uses_crypt(self) -> bool:
        return bool(_bf64_get(self.prop, 61, 1))

    @property
    def dedup(self) -> bool:
        return bool(_bf64_get(self.prop, 62, 1))

    @property
    def byteorder(self) -> int:
        """1 = little-endian (spa.h:471)."""
        return _bf64_get(self.prop, 63, 1)

    @property
    def fill(self) -> int:
        if self.embedded:
            return 1
        return self.fill_word

    @property
    def birth(self) -> int:
        """spa.h:485 — naissance physique si presente, sinon logique."""
        if self.embedded:
            return 0
        return self.birth_physical or self.birth_logical

    @property
    def is_hole(self) -> bool:
        """Trou : aucun DVA renseigne et pas de donnees embarquees."""
        return not self.embedded and all(d.is_empty for d in self.dvas)

    @property
    def is_gang(self) -> bool:
        return any(d.gang for d in self.dvas if not d.is_empty)

    @property
    def is_encrypted(self) -> bool:
        return self.uses_crypt and self.level <= 0 and bool(self.type & E.DMU_OT_ENCRYPTED) \
            if self.type & E.DMU_OT_NEWTYPE else (self.uses_crypt and self.level <= 0)

    @property
    def copies(self) -> int:
        return sum(1 for d in self.dvas if not d.is_empty)

    # --- noms lisibles ------------------------------------------------------
    @property
    def type_name(self) -> str:
        return object_type_name(self.type)

    @property
    def compress_name(self) -> str:
        return E.ZIO_COMPRESS_NAMES.get(self.compress, f"comp_{self.compress}")

    @property
    def checksum_name(self) -> str:
        return E.ZIO_CHECKSUM_NAMES.get(self.checksum, f"cksum_{self.checksum}")

    def embedded_payload(self) -> bytes:
        """Reconstitue la charge utile d'un blkptr embarque (spa.h:269-292)."""
        if not self.embedded:
            raise ValueError("ce blkptr n'est pas embarque")
        mots = struct.unpack("<16Q", self.raw)
        data = b"".join(struct.pack("<Q", mots[i]) for i in BPE_PAYLOAD_WORDS)
        return data[:self.embedded_psize]

    def as_dict(self) -> dict[str, Any]:
        d = {
            "hole": self.is_hole, "embedded": self.embedded,
            "gang": self.is_gang, "level": self.level,
            "type": self.type, "type_name": self.type_name,
            "lsize": self.lsize, "psize": self.psize,
            "compress": self.compress_name, "checksum": self.checksum_name,
            "byteorder": "little" if self.byteorder else "big",
            "dedup": self.dedup, "encrypted": bool(self.uses_crypt),
            "birth_logical": self.birth_logical,
            "birth_physical_stored": self.birth_physical,
            "birth_physical_effective": self.birth,
            "fill": self.fill, "copies": self.copies,
            "cksum": [f"{w:016x}" for w in self.cksum],
            "dvas": [dva.as_dict() for dva in self.dvas if not dva.is_empty],
        }
        if self.embedded:
            d["embedded_type"] = self.embedded_type
            d["embedded_psize"] = self.embedded_psize
        return d

    def __str__(self) -> str:
        if self.is_hole:
            return "<trou>"
        if self.embedded:
            return (f"EMBEDDED et={self.embedded_type} {self.lsize:x}L/"
                    f"{self.embedded_psize:x}P B={self.birth_logical}")
        dvas = " ".join(str(d) for d in self.dvas if not d.is_empty)
        return (f"{dvas} [L{self.level} {self.type_name}] "
                f"{self.checksum_name} {self.compress_name} "
                f"size={self.lsize:x}L/{self.psize:x}P "
                f"birth={self.birth_logical}L/{self.birth}P "
                f"fill={self.fill}")


def parse_blkptr(raw: bytes, offset: int = 0) -> BlockPointer:
    if len(raw) - offset < BLKPTR_SIZE:
        raise ValueError("tampon trop court pour un blkptr")
    mots = struct.unpack_from("<16Q", raw, offset)
    return BlockPointer(
        raw=bytes(raw[offset:offset + BLKPTR_SIZE]),
        dvas=[DVA(mots[0], mots[1]), DVA(mots[2], mots[3]), DVA(mots[4], mots[5])],
        prop=mots[6],
        pad=(mots[7], mots[8]),
        birth_physical=mots[9],
        birth_logical=mots[10],
        fill_word=mots[11],
        cksum=(mots[12], mots[13], mots[14], mots[15]),
    )
