"""
Decompression des blocs ZFS.

Reference normative : OpenZFS 2.3.9
  * table des algorithmes   module/zfs/zio_compress.c:49-80
  * LZJB                    module/zfs/lzjb.c:106-135
  * ZLE (niveau 64)         module/zfs/zle.c:70-94
  * LZ4                     module/zfs/lz4.c  (prefixe de 4 octets big-endian)
  * gzip                    zlib standard
  * zstd                    module/zstd/zfs_zstd.c (en-tete de 8 octets BE)

Regle du projet : si un algorithme n'est pas disponible, on le DIT — on ne
rend jamais des donnees approximatives.
"""

from __future__ import annotations

import struct
import zlib

from . import zfs_enums as E

ZIO_COMPRESS_OFF = 2
ZIO_COMPRESS_LZJB = 3
ZIO_COMPRESS_EMPTY = 4
ZIO_COMPRESS_GZIP_1 = 5
ZIO_COMPRESS_GZIP_9 = 13
ZIO_COMPRESS_ZLE = 14
ZIO_COMPRESS_LZ4 = 15
ZIO_COMPRESS_ZSTD = 16


class DecompressionError(Exception):
    pass


class CompressionNonSupportee(DecompressionError):
    pass


# --- LZ4 ----------------------------------------------------------------------
def lz4_block_decompress(src: bytes, d_len: int) -> bytes:
    """
    Decodeur de bloc LZ4 (format standard, tel qu'utilise par ZFS).
    `src` ne contient PAS le prefixe de longueur de ZFS.
    """
    out = bytearray()
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if pos >= n:
                    raise DecompressionError("LZ4 : fin prematuree (litteraux)")
                b = src[pos]
                pos += 1
                lit += b
                if b != 255:
                    break
        if pos + lit > n:
            raise DecompressionError("LZ4 : litteraux hors tampon")
        out += src[pos:pos + lit]
        pos += lit
        if pos == n:
            break
        if pos + 2 > n:
            raise DecompressionError("LZ4 : offset tronque")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise DecompressionError(f"LZ4 : offset invalide {offset}")
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                if pos >= n:
                    raise DecompressionError("LZ4 : fin prematuree (match)")
                b = src[pos]
                pos += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        depart = len(out) - offset
        for i in range(mlen):
            out.append(out[depart + i])       # recouvrement autorise
        if len(out) > d_len:
            break
    if len(out) < d_len:
        raise DecompressionError(
            f"LZ4 : {len(out)} octets produits, {d_len} attendus")
    return bytes(out[:d_len])


def lz4_decompress(src: bytes, d_len: int) -> bytes:
    """module/zfs/lz4.c — longueur compressee sur 4 octets big-endian en tete."""
    if len(src) < 4:
        raise DecompressionError("LZ4 : bloc trop court")
    bufsiz = struct.unpack_from(">I", src, 0)[0]
    if bufsiz + 4 > len(src):
        raise DecompressionError(
            f"LZ4 : longueur annoncee {bufsiz} incoherente avec {len(src)} octets")
    return lz4_block_decompress(src[4:4 + bufsiz], d_len)


# --- LZJB ---------------------------------------------------------------------
MATCH_BITS = 6
MATCH_MIN = 3
OFFSET_MASK = (1 << (16 - MATCH_BITS)) - 1
NBBY = 8


def lzjb_decompress(src: bytes, d_len: int) -> bytes:
    """module/zfs/lzjb.c:106 — zfs_lzjb_decompress_buf."""
    out = bytearray()
    s = 0
    copymap = 0
    copymask = 1 << (NBBY - 1)
    while len(out) < d_len:
        copymask <<= 1
        if copymask == (1 << NBBY):
            copymask = 1
            if s >= len(src):
                raise DecompressionError("LZJB : fin prematuree (copymap)")
            copymap = src[s]
            s += 1
        if copymap & copymask:
            if s + 2 > len(src):
                raise DecompressionError("LZJB : fin prematuree (match)")
            mlen = (src[s] >> (NBBY - MATCH_BITS)) + MATCH_MIN
            offset = ((src[s] << NBBY) | src[s + 1]) & OFFSET_MASK
            s += 2
            depart = len(out) - offset
            if depart < 0:
                raise DecompressionError("LZJB : offset avant le debut du tampon")
            for _ in range(mlen):
                if len(out) >= d_len:
                    break
                out.append(out[depart])
                depart += 1
        else:
            if s >= len(src):
                raise DecompressionError("LZJB : fin prematuree (litteral)")
            out.append(src[s])
            s += 1
    return bytes(out)


# --- ZLE ----------------------------------------------------------------------
def zle_decompress(src: bytes, d_len: int, n: int = 64) -> bytes:
    """module/zfs/zle.c:70 — niveau 64 (zio_compress.c:74)."""
    out = bytearray()
    s = 0
    while s < len(src) and len(out) < d_len:
        length = 1 + src[s]
        s += 1
        if length <= n:
            if s + length > len(src) or len(out) + length > d_len:
                raise DecompressionError("ZLE : longueur hors tampon")
            out += src[s:s + length]
            s += length
        else:
            length -= n
            if len(out) + length > d_len:
                raise DecompressionError("ZLE : sequence de zeros hors tampon")
            out += b"\x00" * length
    if len(out) != d_len:
        raise DecompressionError(
            f"ZLE : {len(out)} octets produits, {d_len} attendus")
    return bytes(out)


# --- gzip ---------------------------------------------------------------------
def gzip_decompress(src: bytes, d_len: int) -> bytes:
    try:
        out = zlib.decompress(src)
    except zlib.error as exc:
        raise DecompressionError(f"gzip : {exc}") from exc
    if len(out) != d_len:
        raise DecompressionError(
            f"gzip : {len(out)} octets produits, {d_len} attendus")
    return out


# --- zstd ---------------------------------------------------------------------
def _zstd_module():
    try:
        import zstandard            # noqa: F401
        return zstandard
    except ImportError:
        return None


ZSTD_DISPONIBLE = _zstd_module() is not None


def zstd_decompress(src: bytes, d_len: int) -> bytes:
    """
    module/zstd/zfs_zstd.c — en-tete ZFS de 8 octets big-endian :
        uint32 c_len            taille compressee
        uint32 raw_version_level version zstd + niveau
    puis le flux zstd brut.
    """
    mod = _zstd_module()
    if mod is None:
        raise CompressionNonSupportee(
            "compression zstd : aucun decodeur disponible. "
            "Installer le paquet python3-zstandard "
            "(sudo apt install python3-zstandard) puis relancer.")
    if len(src) < 8:
        raise DecompressionError("zstd : bloc trop court")
    c_len, _raw = struct.unpack_from(">II", src, 0)
    if c_len + 8 > len(src):
        raise DecompressionError(
            f"zstd : longueur annoncee {c_len} incoherente avec {len(src)} octets")
    out = mod.ZstdDecompressor().decompress(src[8:8 + c_len],
                                            max_output_size=d_len)
    if len(out) != d_len:
        raise DecompressionError(
            f"zstd : {len(out)} octets produits, {d_len} attendus")
    return out


# --- point d'entree -----------------------------------------------------------
def decompress(kind: int, src: bytes, lsize: int) -> bytes:
    """
    Decompresse `src` (donnees physiques d'un bloc) vers `lsize` octets.
    Leve CompressionNonSupportee plutot que de rendre des donnees douteuses.
    """
    if kind in (0, 1, ZIO_COMPRESS_OFF):
        if len(src) < lsize:
            raise DecompressionError(
                f"bloc non compresse trop court : {len(src)} < {lsize}")
        return src[:lsize]
    if kind == ZIO_COMPRESS_EMPTY:
        return b"\x00" * lsize
    if kind == ZIO_COMPRESS_LZJB:
        return lzjb_decompress(src, lsize)
    if ZIO_COMPRESS_GZIP_1 <= kind <= ZIO_COMPRESS_GZIP_9:
        return gzip_decompress(src, lsize)
    if kind == ZIO_COMPRESS_ZLE:
        return zle_decompress(src, lsize)
    if kind == ZIO_COMPRESS_LZ4:
        return lz4_decompress(src, lsize)
    if kind == ZIO_COMPRESS_ZSTD:
        return zstd_decompress(src, lsize)
    raise CompressionNonSupportee(
        f"algorithme de compression inconnu : "
        f"{E.ZIO_COMPRESS_NAMES.get(kind, kind)} ({kind})")
