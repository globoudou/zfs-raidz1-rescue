"""
Lecture et validation des labels de vdev ZFS.

Reference normative : OpenZFS 2.3.9
  * include/sys/vdev_impl.h:474-558   (geometrie du label)
  * module/zfs/vdev_label.c:163-170   (vdev_label_offset)
  * module/zfs/zio_checksum.c:289-403 (checksum embarque, verifieur = offset)
  * module/zfs/sha2_zfs.c:44-78       (SHA-256 : mots stockes en BE_64)

Geometrie d'un label (256 Kio) :
    0      vl_pad1        8 Kio
    8K     vl_be          8 Kio
    16K    vl_vdev_phys   112 Kio  -> nvlist (112K-40) + zio_eck_t (40)
    128K   vl_uberblock   128 Kio  -> anneau d'uberblocks

Emplacement des 4 labels sur le support (vdev_label_offset) :
    L0 = 0                    L1 = 256 Kio
    L2 = psize - 512 Kio      L3 = psize - 256 Kio
avec psize = taille du support alignee (arrondie vers le bas) sur 256 Kio.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from . import consts
from .nvlist import Nvlist, NvlistError, parse_packed_nvlist
from .readonly import ReadOnlyDevice


def label_offset(psize: int, index: int, offset_in_label: int = 0) -> int:
    """module/zfs/vdev_label.c:163 — vdev_label_offset()."""
    if not 0 <= index < consts.VDEV_LABELS:
        raise ValueError(f"index de label invalide : {index}")
    if psize % consts.VDEV_LABEL_SIZE:
        raise ValueError("psize doit etre un multiple de 256 Kio")
    base = 0 if index < consts.VDEV_LABELS // 2 else (
        psize - consts.VDEV_LABELS * consts.VDEV_LABEL_SIZE)
    return offset_in_label + index * consts.VDEV_LABEL_SIZE + base


def uberblock_shift(ashift: int) -> int:
    """include/sys/vdev_impl.h:487 — MIN(MAX(ashift, 10), 13)."""
    return min(max(ashift, consts.UBERBLOCK_SHIFT), consts.MAX_UBERBLOCK_SHIFT)


# --- checksum embarque (zio_eck_t) -------------------------------------------
@dataclass
class EmbeddedChecksum:
    magic_ok: bool
    byteswapped: bool          # le label a ete ecrit par une machine d'endianness opposee
    stored: tuple[int, int, int, int]
    computed: tuple[int, int, int, int]

    @property
    def valid(self) -> bool:
        return self.magic_ok and self.stored == self.computed

    def as_dict(self) -> dict[str, Any]:
        return {
            "magic_ok": self.magic_ok,
            "byteswapped": self.byteswapped,
            "valid": self.valid,
            "stored": [f"{w:016x}" for w in self.stored],
            "computed": [f"{w:016x}" for w in self.computed],
        }


def verify_embedded_checksum(buf: bytes, device_offset: int) -> EmbeddedChecksum:
    """
    Verifie le checksum auto-porte d'une region de label (ZIO_CHECKSUM_LABEL).

    Procedure exacte d'OpenZFS (zio_checksum_compute) :
      1. l'eck occupe les 40 derniers octets de la region ;
      2. on ecrit zec_magic = ZEC_MAGIC ;
      3. on ecrit zec_cksum = (offset_physique, 0, 0, 0)  <- le "verifieur" ;
      4. SHA-256 sur toute la region ainsi preparee ;
      5. les 4 mots resultats sont stockes en BE_64 (sha2_zfs.c).
    """
    eck_off = len(buf) - consts.ZIO_ECK_SIZE
    magic_le = struct.unpack_from("<Q", buf, eck_off)[0]
    magic_be = struct.unpack_from(">Q", buf, eck_off)[0]

    if magic_le == consts.ZEC_MAGIC:
        byteswapped, endian = False, "<"
    elif magic_be == consts.ZEC_MAGIC:
        byteswapped, endian = True, ">"
    else:
        return EmbeddedChecksum(False, False, (0, 0, 0, 0), (0, 0, 0, 0))

    stored = struct.unpack_from(endian + "4Q", buf, eck_off + 8)

    prepared = bytearray(buf)
    struct.pack_into(endian + "Q", prepared, eck_off, consts.ZEC_MAGIC)
    struct.pack_into(endian + "4Q", prepared, eck_off + 8, device_offset, 0, 0, 0)
    digest = hashlib.sha256(bytes(prepared)).digest()
    computed = struct.unpack(">4Q", digest)      # sha2_zfs.c : BE_64(mot)

    return EmbeddedChecksum(True, byteswapped, tuple(stored), computed)


# --- label --------------------------------------------------------------------
@dataclass
class VdevLabel:
    index: int
    offset: int                       # offset absolu du label sur le support
    present: bool = False             # au moins lisible
    nvlist: Nvlist | None = None
    nvlist_encoding: str | None = None
    nvlist_endian: str | None = None
    checksum: EmbeddedChecksum | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def config(self) -> dict[str, Any]:
        return self.nvlist.to_dict() if self.nvlist else {}

    @property
    def valid(self) -> bool:
        return (self.present and self.nvlist is not None
                and self.checksum is not None and self.checksum.valid)

    @property
    def txg(self) -> int | None:
        return self.config.get("txg")

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "present": self.present,
            "valid": self.valid,
            "nvlist_encoding": self.nvlist_encoding,
            "nvlist_endian": self.nvlist_endian,
            "checksum": self.checksum.as_dict() if self.checksum else None,
            "errors": self.errors,
            "config": self.config,
            "anomalies_nvlist": self.nvlist.anomalies if self.nvlist else [],
        }


def read_label(dev: ReadOnlyDevice, index: int) -> VdevLabel:
    psize = dev.info.psize
    off = label_offset(psize, index)
    lbl = VdevLabel(index=index, offset=off)

    phys_off = off + consts.OFFSETOF_VDEV_PHYS
    try:
        raw = dev.pread(phys_off, consts.VDEV_PHYS_SIZE)
    except Exception as exc:                       # secteur illisible, image tronquee
        lbl.errors.append(f"lecture impossible : {exc}")
        return lbl
    lbl.present = True

    lbl.checksum = verify_embedded_checksum(raw, phys_off)

    erreur_nvlist = None
    try:
        nvl, hdr, _ = parse_packed_nvlist(
            raw[:consts.VDEV_PHYS_SIZE - consts.ZIO_ECK_SIZE])
        lbl.nvlist = nvl
        lbl.nvlist_encoding = hdr.encoding_name
        lbl.nvlist_endian = hdr.endian_name
    except NvlistError as exc:
        erreur_nvlist = str(exc)

    # Un diagnostic par situation, plutot que d'empiler des messages techniques
    # qui donneraient l'impression d'un label abime alors qu'il n'y en a aucun.
    if not lbl.checksum.magic_ok and erreur_nvlist:
        vide = raw.count(0) == len(raw)
        lbl.errors.append(
            "emplacement de label vide" if vide else
            "emplacement de label non initialise : ni checksum ni configuration "
            "exploitables (ce support ne porte probablement pas de vdev ici)")
    elif not lbl.checksum.magic_ok:
        lbl.errors.append(
            "checksum absent mais configuration lisible : label partiellement "
            "ecrase")
    else:
        if not lbl.checksum.valid:
            lbl.errors.append("checksum SHA-256 non conforme")
        if erreur_nvlist:
            lbl.errors.append(f"nvlist illisible : {erreur_nvlist}")

    return lbl


def read_all_labels(dev: ReadOnlyDevice) -> list[VdevLabel]:
    """Lit les 4 labels. Un label absent ou casse n'interrompt jamais la lecture."""
    out = []
    for i in range(consts.VDEV_LABELS):
        try:
            out.append(read_label(dev, i))
        except Exception as exc:
            lbl = VdevLabel(index=i, offset=-1)
            lbl.errors.append(f"erreur inattendue : {exc}")
            out.append(lbl)
    return out
