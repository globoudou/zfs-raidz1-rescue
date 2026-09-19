"""
Checksums ZFS.

Reference normative : OpenZFS 2.3.9
  * fletcher 2 et 4   module/zcommon/zfs_fletcher.c:238-345
  * SHA-256           module/zfs/sha2_zfs.c:44-78  (mots stockes en BE_64)
  * verification      module/zfs/zio_checksum.c:422-520
  * BP_SHOULD_BYTESWAP include/sys/spa.h:605

Les checksums ZFS sont des quadruplets d'entiers 64 bits (zio_cksum_t).
Le checksum d'un bloc porte sur ses donnees PHYSIQUES (donc compressees),
de taille psize, et se compare a blk_cksum du block pointer.
"""

from __future__ import annotations

import hashlib
import struct
import sys

from . import zfs_enums as E

M64 = (1 << 64) - 1
ZFS_HOST_BYTEORDER = 1 if sys.byteorder == "little" else 0

Cksum = tuple[int, int, int, int]


def fletcher_4(data: bytes, byteswap: bool = False) -> Cksum:
    """zfs_fletcher.c:318 — fletcher_4_scalar_native/byteswap (mots de 32 bits)."""
    n = len(data) // 4
    mots = struct.unpack_from((">" if byteswap else "<") + f"{n}I", data, 0)
    a = b = c = d = 0
    for x in mots:
        a = (a + x) & M64
        b = (b + a) & M64
        c = (c + b) & M64
        d = (d + c) & M64
    return (a, b, c, d)


def fletcher_2(data: bytes, byteswap: bool = False) -> Cksum:
    """zfs_fletcher.c:238 — fletcher_2_incremental_native (paires de mots 64 bits)."""
    n = len(data) // 8
    mots = struct.unpack_from((">" if byteswap else "<") + f"{n}Q", data, 0)
    a0 = a1 = b0 = b1 = 0
    for i in range(0, n - 1, 2):
        a0 = (a0 + mots[i]) & M64
        a1 = (a1 + mots[i + 1]) & M64
        b0 = (b0 + a0) & M64
        b1 = (b1 + a1) & M64
    return (a0, a1, b0, b1)


def sha256(data: bytes, byteswap: bool = False) -> Cksum:
    """sha2_zfs.c:44 — les 4 mots sont stockes en BE_64 du condense."""
    return struct.unpack(">4Q", hashlib.sha256(data).digest())


# Indices : enum zio_checksum (include/sys/zio.h:78), voir zfs_enums.py
FONCTIONS = {
    6: fletcher_2,     # ZIO_CHECKSUM_FLETCHER_2
    7: fletcher_4,     # ZIO_CHECKSUM_FLETCHER_4
    8: sha256,         # ZIO_CHECKSUM_SHA256
}

NON_SUPPORTES = {11: "sha512", 12: "skein", 13: "edonr", 14: "blake3"}


class ChecksumNonSupporte(Exception):
    pass


def compute(kind: int, data: bytes, byteswap: bool = False) -> Cksum:
    fn = FONCTIONS.get(kind)
    if fn is None:
        nom = E.ZIO_CHECKSUM_NAMES.get(kind, str(kind))
        raise ChecksumNonSupporte(
            f"algorithme de checksum non implemente : {nom} ({kind})")
    return fn(data, byteswap)


def verify(kind: int, data: bytes, expected: Cksum, byteswap: bool = False
           ) -> tuple[bool, Cksum]:
    """
    Retourne (conforme, checksum_calcule).
    `byteswap` doit valoir BP_SHOULD_BYTESWAP(bp) : le bloc a ete ecrit par une
    machine d'endianness opposee a la notre.
    """
    if kind in (0, 1, 2, 10):        # inherit / on / off / noparity
        return True, tuple(expected)
    calcule = compute(kind, data, byteswap)
    attendu = tuple(expected)
    if byteswap:
        attendu = tuple(int.from_bytes(struct.pack("<Q", w), "big")
                        for w in attendu)
    return calcule == attendu, calcule
