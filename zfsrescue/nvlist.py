"""
Decodeur de nvlist ZFS (encodage XDR).

Reference normative : module/nvpair/nvpair.c d'OpenZFS 2.3.9.

Format (commentaire nvpair.c:3205-3224) :

    en-tete nvs_header_t (4 octets) : encoding, endian, reserved1, reserved2
    nvl_version (int32)
    nvl_nvflag  (uint32)
    puis, pour chaque nvpair :
        encoded_size (int32)   taille du couple encode
        decoded_size (int32)   taille en memoire
        name         (string XDR : int32 longueur sans NUL + octets + padding 4)
        type         (int32, data_type_t)
        nelem        (int32)
        data
    fin de liste : deux int32 a zero (8 octets)

Points de vigilance verifies dans la source (nvs_xdr_nvp_op, nvpair.c:3357+) :
  * XDR est TOUJOURS big-endian ;
  * int8/uint8/byte/int16/uint16 sont encodes sur 4 octets (xdr_char/xdr_short) ;
  * les tableaux typent passent par xdr_array() qui prefixe un compteur uint32 ;
  * BYTE_ARRAY passe par xdr_opaque() : nelem octets bruts + padding a 4 ;
  * NVLIST / NVLIST_ARRAY sont encodes en ligne (recursivement), sans en-tete.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from . import consts


class NvlistError(Exception):
    pass


def _as_signed(v: int, bits: int) -> int:
    """Interprete les `bits` de poids faible de v comme un entier signe."""
    sign = 1 << (bits - 1)
    return (v & (sign - 1)) - (v & sign)


def _roundup4(n: int) -> int:
    return (n + 3) & ~3


class _XdrReader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise NvlistError(
                f"depassement de tampon : {self.pos}+{n} > {len(self.buf)}"
            )
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def int32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def uint32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def int64(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def uint64(self) -> int:
        return struct.unpack(">Q", self._take(8))[0]

    def double(self) -> float:
        return struct.unpack(">d", self._take(8))[0]

    def string(self) -> str:
        n = self.uint32()
        raw = self._take(_roundup4(n))[:n]
        return raw.decode("utf-8", "surrogateescape")

    def opaque(self, n: int) -> bytes:
        return self._take(_roundup4(n))[:n]


@dataclass
class NvPair:
    name: str
    type: int
    nelem: int
    value: Any

    @property
    def type_name(self) -> str:
        return consts.DATA_TYPE_NAMES.get(self.type, f"type_{self.type}")


@dataclass
class Nvlist:
    version: int
    nvflag: int
    pairs: list[NvPair] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Vue simple nom -> valeur (les nvlist imbriquees sont converties)."""
        out: dict[str, Any] = {}
        for p in self.pairs:
            out[p.name] = _plain(p.value)
        return out

    def get(self, name: str, default: Any = None) -> Any:
        for p in self.pairs:
            if p.name == name:
                return p.value
        return default


def _plain(v: Any) -> Any:
    if isinstance(v, Nvlist):
        return v.to_dict()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


# --- decodage d'une valeur ----------------------------------------------------
def _read_value(r: _XdrReader, dtype: int, nelem: int) -> Any:
    D = consts
    if dtype == D.DATA_TYPE_BOOLEAN:
        return True
    if nelem == 0:
        # nvs_xdr_nvp_op : "if there is no data to extract then return"
        return None

    # xdr_char encode un char sur 4 octets AVEC extension de signe :
    # 200 (uint8) est ecrit 0xFFFFFFC8. On ne garde donc que l'octet de poids
    # faible, puis on applique le signe voulu par le type declare.
    if dtype in (D.DATA_TYPE_BYTE, D.DATA_TYPE_UINT8):
        return r.int32() & 0xFF
    if dtype == D.DATA_TYPE_INT8:
        return _as_signed(r.int32() & 0xFF, 8)
    if dtype == D.DATA_TYPE_INT16:
        return _as_signed(r.int32() & 0xFFFF, 16)   # xdr_short -> 4 octets
    if dtype == D.DATA_TYPE_UINT16:
        return r.uint32()
    if dtype in (D.DATA_TYPE_INT32, D.DATA_TYPE_BOOLEAN_VALUE):
        return r.int32()
    if dtype == D.DATA_TYPE_UINT32:
        return r.uint32()
    if dtype in (D.DATA_TYPE_INT64, D.DATA_TYPE_HRTIME):
        return r.int64()
    if dtype == D.DATA_TYPE_UINT64:
        return r.uint64()
    if dtype == D.DATA_TYPE_DOUBLE:
        return r.double()
    if dtype == D.DATA_TYPE_STRING:
        return r.string()
    if dtype == D.DATA_TYPE_BYTE_ARRAY:
        return r.opaque(nelem)

    # tableaux typiques : xdr_array() prefixe le nombre d'elements
    def _u8():
        return r.int32() & 0xFF

    def _i8():
        return _as_signed(r.int32() & 0xFF, 8)

    def _i16():
        return _as_signed(r.int32() & 0xFFFF, 16)

    array_scalars = {
        D.DATA_TYPE_INT8_ARRAY: _i8, D.DATA_TYPE_UINT8_ARRAY: _u8,
        D.DATA_TYPE_INT16_ARRAY: _i16, D.DATA_TYPE_UINT16_ARRAY: r.uint32,
        D.DATA_TYPE_INT32_ARRAY: r.int32, D.DATA_TYPE_UINT32_ARRAY: r.uint32,
        D.DATA_TYPE_BOOLEAN_ARRAY: r.int32,
        D.DATA_TYPE_INT64_ARRAY: r.int64, D.DATA_TYPE_UINT64_ARRAY: r.uint64,
    }
    if dtype in array_scalars:
        cnt = r.uint32()
        if cnt != nelem:
            raise NvlistError(
                f"tableau incoherent : compteur XDR {cnt} != nelem {nelem}"
            )
        rd = array_scalars[dtype]
        return [rd() for _ in range(cnt)]

    if dtype == D.DATA_TYPE_STRING_ARRAY:
        return [r.string() for _ in range(nelem)]

    if dtype == D.DATA_TYPE_NVLIST:
        return _parse_body(r)

    if dtype == D.DATA_TYPE_NVLIST_ARRAY:
        return [_parse_body(r) for _ in range(nelem)]

    raise NvlistError(f"type nvpair non gere : {dtype}")


def _parse_body(r: _XdrReader) -> Nvlist:
    """Decode version + nvflag + couples jusqu'au marqueur de fin."""
    version = r.int32()
    nvflag = r.uint32()
    nvl = Nvlist(version=version, nvflag=nvflag)

    while True:
        start = r.pos
        encoded_size = r.int32()
        decoded_size = r.int32()
        if encoded_size == 0 and decoded_size == 0:
            break
        if encoded_size < 0 or start + encoded_size > len(r.buf):
            raise NvlistError(
                f"encoded_size invalide ({encoded_size}) a l'offset {start}"
            )

        name = r.string()
        dtype = r.int32()
        nelem = r.int32()
        value = _read_value(r, dtype, nelem)
        nvl.pairs.append(NvPair(name=name, type=dtype, nelem=nelem, value=value))

        # Resynchronisation forensic : on fait confiance a encoded_size, qui
        # permet de continuer meme si un type est mal interprete.
        expected_end = start + encoded_size
        if r.pos != expected_end:
            nvl.anomalies.append(
                f"'{name}': fin de couple a {r.pos}, attendue a {expected_end} "
                f"(encoded_size={encoded_size})"
            )
            r.pos = expected_end
    return nvl


@dataclass
class NvlistHeader:
    encoding: int
    endian: int

    @property
    def encoding_name(self) -> str:
        return {0: "native", 1: "xdr"}.get(self.encoding, f"inconnu({self.encoding})")

    @property
    def endian_name(self) -> str:
        return {0: "big", 1: "little"}.get(self.endian, f"inconnu({self.endian})")


def parse_packed_nvlist(buf: bytes) -> tuple[Nvlist, NvlistHeader, int]:
    """
    Decode un nvlist « packe » complet (en-tete nvs_header_t inclus).
    Retourne (nvlist, en-tete, nombre d'octets consommes).
    """
    if len(buf) < 4:
        raise NvlistError("tampon trop court pour un en-tete nvlist")
    encoding, endian = buf[0], buf[1]
    hdr = NvlistHeader(encoding=encoding, endian=endian)
    if encoding != consts.NV_ENCODE_XDR:
        raise NvlistError(
            f"encodage nvlist non gere : {hdr.encoding_name} "
            "(seul XDR est utilise dans les labels ZFS)"
        )
    r = _XdrReader(buf, 4)
    nvl = _parse_body(r)
    return nvl, hdr, r.pos
