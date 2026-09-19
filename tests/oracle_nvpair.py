"""
Oracle independant : construit des nvlist avec la VRAIE bibliotheque OpenZFS
(libnvpair.so) et les encode en XDR, pour valider notre decodeur.

Ce n'est pas une reimplementation : c'est le code d'OpenZFS lui-meme qui
produit les octets de reference.
"""

from __future__ import annotations

import ctypes
import ctypes.util

NV_ENCODE_NATIVE = 0
NV_ENCODE_XDR = 1
NV_UNIQUE_NAME = 0x1


def _load():
    for cand in (ctypes.util.find_library("nvpair"),
                 "libnvpair.so.3", "libnvpair.so"):
        if not cand:
            continue
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    return None


_lib = _load()
available = _lib is not None


class NvlistBuilder:
    """Enveloppe minimale autour de nvlist_alloc/nvlist_add_*/nvlist_pack."""

    def __init__(self):
        if not available:
            raise RuntimeError("libnvpair indisponible")
        self._p = ctypes.c_void_p()
        rc = _lib.nvlist_alloc(ctypes.byref(self._p), NV_UNIQUE_NAME, 0)
        if rc != 0:
            raise RuntimeError(f"nvlist_alloc a echoue : {rc}")

    # -- ajouts -------------------------------------------------------------
    def _check(self, rc, what):
        if rc != 0:
            raise RuntimeError(f"{what} a echoue : {rc}")

    def add_boolean(self, name):
        self._check(_lib.nvlist_add_boolean(self._p, name.encode()),
                    "nvlist_add_boolean")

    def add_boolean_value(self, name, val: bool):
        self._check(_lib.nvlist_add_boolean_value(self._p, name.encode(),
                                                  ctypes.c_int(1 if val else 0)),
                    "nvlist_add_boolean_value")

    def add_uint64(self, name, val: int):
        self._check(_lib.nvlist_add_uint64(self._p, name.encode(),
                                           ctypes.c_uint64(val)),
                    "nvlist_add_uint64")

    def add_int64(self, name, val: int):
        self._check(_lib.nvlist_add_int64(self._p, name.encode(),
                                          ctypes.c_int64(val)),
                    "nvlist_add_int64")

    def add_uint32(self, name, val: int):
        self._check(_lib.nvlist_add_uint32(self._p, name.encode(),
                                           ctypes.c_uint32(val)),
                    "nvlist_add_uint32")

    def add_int32(self, name, val: int):
        self._check(_lib.nvlist_add_int32(self._p, name.encode(),
                                          ctypes.c_int32(val)),
                    "nvlist_add_int32")

    def add_int16(self, name, val: int):
        self._check(_lib.nvlist_add_int16(self._p, name.encode(),
                                          ctypes.c_int16(val)),
                    "nvlist_add_int16")

    def add_uint8(self, name, val: int):
        self._check(_lib.nvlist_add_uint8(self._p, name.encode(),
                                          ctypes.c_uint8(val)),
                    "nvlist_add_uint8")

    def add_string(self, name, val: str):
        self._check(_lib.nvlist_add_string(self._p, name.encode(), val.encode()),
                    "nvlist_add_string")

    def add_uint64_array(self, name, vals):
        arr = (ctypes.c_uint64 * len(vals))(*vals)
        self._check(_lib.nvlist_add_uint64_array(self._p, name.encode(), arr,
                                                 ctypes.c_uint(len(vals))),
                    "nvlist_add_uint64_array")

    def add_uint8_array(self, name, vals):
        arr = (ctypes.c_uint8 * len(vals))(*vals)
        self._check(_lib.nvlist_add_uint8_array(self._p, name.encode(), arr,
                                                ctypes.c_uint(len(vals))),
                    "nvlist_add_uint8_array")

    def add_byte_array(self, name, data: bytes):
        arr = (ctypes.c_ubyte * len(data))(*data)
        self._check(_lib.nvlist_add_byte_array(self._p, name.encode(), arr,
                                               ctypes.c_uint(len(data))),
                    "nvlist_add_byte_array")

    def add_string_array(self, name, vals):
        arr = (ctypes.c_char_p * len(vals))(*[v.encode() for v in vals])
        self._check(_lib.nvlist_add_string_array(self._p, name.encode(), arr,
                                                 ctypes.c_uint(len(vals))),
                    "nvlist_add_string_array")

    def add_nvlist(self, name, other: "NvlistBuilder"):
        self._check(_lib.nvlist_add_nvlist(self._p, name.encode(), other._p),
                    "nvlist_add_nvlist")

    def add_nvlist_array(self, name, others):
        arr = (ctypes.c_void_p * len(others))(*[o._p for o in others])
        self._check(_lib.nvlist_add_nvlist_array(self._p, name.encode(), arr,
                                                 ctypes.c_uint(len(others))),
                    "nvlist_add_nvlist_array")

    # -- encodage -----------------------------------------------------------
    def pack_xdr(self) -> bytes:
        buf = ctypes.POINTER(ctypes.c_char)()
        size = ctypes.c_size_t(0)
        rc = _lib.nvlist_pack(self._p, ctypes.byref(buf), ctypes.byref(size),
                              ctypes.c_int(NV_ENCODE_XDR), ctypes.c_int(0))
        if rc != 0:
            raise RuntimeError(f"nvlist_pack a echoue : {rc}")
        return ctypes.string_at(buf, size.value)

    def free(self):
        if self._p:
            _lib.nvlist_free(self._p)
            self._p = ctypes.c_void_p()
