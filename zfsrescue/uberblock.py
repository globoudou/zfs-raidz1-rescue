"""
Lecture des uberblocks ZFS.

Reference normative : OpenZFS 2.3.9
  * struct uberblock             include/sys/uberblock_impl.h:122-185
  * emplacement dans le label    include/sys/vdev_impl.h:486-495
  * ecriture / choix du creneau  module/zfs/vdev_label.c:1746-1806
  * checksum LABEL auto-porte    module/zfs/zio_checksum.c:363-403
  * etat d'expansion RAIDZ       include/sys/uberblock_impl.h:98-120

Chaque label contient un anneau de 128 Kio d'uberblocks. La taille d'un
creneau vaut 2^MIN(MAX(ashift,10),13) : 32 creneaux de 4096 octets pour
ashift=12. Un uberblock de txg T occupe le creneau T % nombre_de_creneaux
(moins un creneau si MMP est actif).

Disposition (216 octets, le reste du creneau est mis a zero) :
    0   ub_magic          0x00bab10c
    8   ub_version
    16  ub_txg
    24  ub_guid_sum       somme de TOUS les guid de vdev de l'arbre
    32  ub_timestamp
    40  ub_rootbp         blkptr_t (128 octets) -> objset du MOS
    168 ub_software_version
    176 ub_mmp_magic
    184 ub_mmp_delay
    192 ub_mmp_config
    200 ub_checkpoint_txg
    208 ub_raidz_reflow_info
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from typing import Any

from . import consts
from .blkptr import BLKPTR_SIZE, BlockPointer, parse_blkptr
from .label import EmbeddedChecksum, label_offset, uberblock_shift, verify_embedded_checksum
from .readonly import ReadOnlyDevice

UBERBLOCK_STRUCT_SIZE = 216
MMP_MAGIC = 0xA11CEA11

RRSS_STATE_NAMES = {
    0: "NOT_IN_USE", 1: "SCRATCH_VALID", 2: "INVALID_SYNCED",
    3: "INVALID_SYNCED_ON_IMPORT", 4: "INVALID_SYNCED_REFLOW",
}


@dataclass
class Uberblock:
    device: str
    label_index: int
    slot: int
    offset: int                       # offset absolu du creneau sur le support
    slot_size: int
    magic_ok: bool = False
    byteswapped: bool = False
    checksum: EmbeddedChecksum | None = None
    version: int = 0
    txg: int = 0
    guid_sum: int = 0
    timestamp: int = 0
    rootbp: BlockPointer | None = None
    software_version: int = 0
    mmp_magic: int = 0
    mmp_delay: int = 0
    mmp_config: int = 0
    checkpoint_txg: int = 0
    raidz_reflow_info: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return (self.magic_ok and self.checksum is not None
                and self.checksum.valid and self.rootbp is not None)

    @property
    def timestamp_iso(self) -> str:
        if not self.timestamp:
            return ""
        return _dt.datetime.fromtimestamp(
            self.timestamp, _dt.timezone.utc).isoformat()

    @property
    def raidz_reflow_state(self) -> int:
        """uberblock_impl.h:111 — RRSS_GET_STATE."""
        return (self.raidz_reflow_info >> 55) & 0x1FF

    @property
    def raidz_reflow_offset(self) -> int:
        """uberblock_impl.h:106 — RRSS_GET_OFFSET."""
        return (self.raidz_reflow_info & ((1 << 55) - 1)) << 9

    @property
    def raidz_expansion_active(self) -> bool:
        """Vrai si le pool a connu / connait une expansion RAIDZ (hors perimetre)."""
        return self.raidz_reflow_info != 0

    @property
    def mmp_valid(self) -> bool:
        return self.mmp_magic == MMP_MAGIC

    @property
    def mmp_enabled(self) -> bool:
        return self.mmp_valid and self.mmp_delay != 0

    @property
    def identity(self) -> tuple:
        """Deux copies du meme uberblock partagent cette identite."""
        return (self.txg, self.timestamp, self.guid_sum,
                self.rootbp.cksum if self.rootbp else None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device, "label": self.label_index, "slot": self.slot,
            "offset": self.offset, "slot_size": self.slot_size,
            "valid": self.valid, "magic_ok": self.magic_ok,
            "byteswapped": self.byteswapped,
            "checksum": self.checksum.as_dict() if self.checksum else None,
            "version": self.version, "txg": self.txg,
            "guid_sum": self.guid_sum,
            "timestamp": self.timestamp, "timestamp_utc": self.timestamp_iso,
            "software_version": self.software_version,
            "mmp": {"magic_ok": self.mmp_valid, "enabled": self.mmp_enabled,
                    "delay": self.mmp_delay, "config": self.mmp_config},
            "checkpoint_txg": self.checkpoint_txg,
            "raidz_reflow_info": self.raidz_reflow_info,
            "raidz_reflow_state": self.raidz_reflow_state,
            "raidz_reflow_state_name": RRSS_STATE_NAMES.get(
                self.raidz_reflow_state, f"etat_{self.raidz_reflow_state}"),
            "raidz_reflow_offset": self.raidz_reflow_offset,
            "rootbp": self.rootbp.as_dict() if self.rootbp else None,
            "rootbp_text": str(self.rootbp) if self.rootbp else None,
            "errors": self.errors,
        }


def parse_uberblock(raw: bytes, device: str, label_index: int, slot: int,
                    offset: int) -> Uberblock:
    """Decode un creneau d'uberblock et valide son checksum auto-porte."""
    ub = Uberblock(device=device, label_index=label_index, slot=slot,
                   offset=offset, slot_size=len(raw))

    magic_le, = struct.unpack_from("<Q", raw, 0)
    magic_be, = struct.unpack_from(">Q", raw, 0)
    if magic_le == consts.UBERBLOCK_MAGIC:
        ordre = "<"
    elif magic_be == consts.UBERBLOCK_MAGIC:
        ordre, ub.byteswapped = ">", True
    else:
        return ub                       # creneau jamais ecrit
    ub.magic_ok = True

    champs = struct.unpack_from(ordre + "5Q", raw, 0)
    (_magic, ub.version, ub.txg, ub.guid_sum, ub.timestamp) = champs
    ub.rootbp = parse_blkptr(raw, 40)
    (ub.software_version, ub.mmp_magic, ub.mmp_delay, ub.mmp_config,
     ub.checkpoint_txg, ub.raidz_reflow_info) = struct.unpack_from(
        ordre + "6Q", raw, 40 + BLKPTR_SIZE)

    ub.checksum = verify_embedded_checksum(raw, offset)
    if not ub.checksum.valid:
        ub.errors.append("checksum invalide")
    return ub


def read_uberblocks(dev: ReadOnlyDevice, ashift: int) -> list[Uberblock]:
    """
    Lit les 4 anneaux d'uberblocks d'un support.
    Un creneau illisible ou vide n'interrompt jamais le parcours.
    """
    shift = uberblock_shift(ashift)
    taille = 1 << shift
    nombre = consts.VDEV_UBERBLOCK_RING >> shift
    out: list[Uberblock] = []

    for l in range(consts.VDEV_LABELS):
        base = label_offset(dev.info.psize, l, consts.OFFSETOF_UBERBLOCK_RING)
        try:
            anneau = dev.pread(base, consts.VDEV_UBERBLOCK_RING)
        except Exception as exc:
            ub = Uberblock(device=dev.info.path, label_index=l, slot=-1,
                           offset=base, slot_size=taille)
            ub.errors.append(f"anneau illisible : {exc}")
            out.append(ub)
            continue
        for n in range(nombre):
            brut = anneau[n * taille:(n + 1) * taille]
            out.append(parse_uberblock(brut, dev.info.path, l, n,
                                       base + n * taille))
    return out


@dataclass
class UberblockSet:
    """Uberblocks agreges sur l'ensemble des supports fournis."""
    all: list[Uberblock] = field(default_factory=list)

    @property
    def valid(self) -> list[Uberblock]:
        return [u for u in self.all if u.valid]

    @property
    def present(self) -> list[Uberblock]:
        return [u for u in self.all if u.magic_ok]

    @property
    def corrupt(self) -> list[Uberblock]:
        return [u for u in self.all if u.magic_ok and not u.valid]

    def unique_by_txg(self) -> dict[int, list[Uberblock]]:
        out: dict[int, list[Uberblock]] = {}
        for u in self.valid:
            out.setdefault(u.txg, []).append(u)
        return dict(sorted(out.items()))

    @property
    def best(self) -> Uberblock | None:
        """Uberblock valide de txg le plus eleve (spa_ld_select_uberblock)."""
        v = self.valid
        return max(v, key=lambda u: (u.txg, u.timestamp)) if v else None

    def history(self) -> list[dict[str, Any]]:
        """Etats du pool potentiellement exploitables, du plus recent au plus ancien."""
        out = []
        for txg, ubs in sorted(self.unique_by_txg().items(), reverse=True):
            ref = ubs[0]
            identites = {u.identity for u in ubs}
            out.append({
                "txg": txg,
                "timestamp": ref.timestamp,
                "timestamp_utc": ref.timestamp_iso,
                "copies": len(ubs),
                "devices": sorted({u.device for u in ubs}),
                "labels": sorted({u.label_index for u in ubs}),
                "slot": ref.slot,
                "coherent_copies": len(identites) == 1,
                "rootbp": str(ref.rootbp),
                "rootbp_copies": ref.rootbp.copies if ref.rootbp else 0,
                "checkpoint_txg": ref.checkpoint_txg,
                "raidz_reflow_state": ref.raidz_reflow_state,
            })
        return out


def guid_sum_expected(pool_guid: int, top_guids: list[int],
                      leaf_guids: list[int]) -> int:
    """
    Somme attendue des guid (vdev.c:573, 695) : chaque vdev de l'arbre ajoute
    son propre guid, racine comprise. Comparer a ub_guid_sum permet de detecter
    des vdev dont nous ignorons l'existence.
    """
    total = pool_guid + sum(top_guids) + sum(leaf_guids)
    return total % (1 << 64)
