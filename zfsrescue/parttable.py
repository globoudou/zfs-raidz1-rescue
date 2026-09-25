"""
Lecture des tables de partitions (GPT et MBR), en lecture seule.

Un vdev ZFS n'occupe pas toujours un disque entier : FreeBSD, FreeNAS/TrueNAS
et la plupart des installations Linux placent le pool dans une PARTITION. Les
quatre labels se trouvent alors au debut et a la fin de cette partition, pas du
disque. Sans cette lecture, l'outil chercherait les labels au mauvais endroit.

References :
  * UEFI 2.10, section 5.3 (GUID Partition Table)
  * types de partitions : https://en.wikipedia.org/wiki/GUID_Partition_Table
"""

from __future__ import annotations

import binascii
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

GPT_SIGNATURE = b"EFI PART"
TAILLES_SECTEUR = (512, 4096)

# Types de partition susceptibles de contenir un pool ZFS
TYPES_ZFS = {
    "516e7cba-6ecf-11d6-8ff8-00022d09712b": "FreeBSD ZFS",
    "6a898cc3-1dd2-11b2-99a6-080020736631": "Solaris /usr ou Apple ZFS",
    "6a85cf4d-1dd2-11b2-99a6-080020736631": "Solaris root",
    "6a87c46f-1dd2-11b2-99a6-080020736631": "Solaris swap",
}
AUTRES_TYPES = {
    "516e7cb5-6ecf-11d6-8ff8-00022d09712b": "FreeBSD swap",
    "516e7cb6-6ecf-11d6-8ff8-00022d09712b": "FreeBSD UFS",
    "83bd6b9d-7f41-11dc-be0b-001560b84f0f": "FreeBSD boot",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "00000000-0000-0000-0000-000000000000": "(vide)",
}
# Types MBR pouvant contenir du ZFS
TYPES_MBR_ZFS = {0xBF: "Solaris", 0xA5: "FreeBSD"}


@dataclass
class Partition:
    index: int
    start: int                 # offset en octets depuis le debut du support
    size: int                  # taille en octets
    type_id: str               # GUID (GPT) ou code hexadecimal (MBR)
    type_name: str
    name: str = ""

    @property
    def likely_zfs(self) -> bool:
        return (self.type_id in TYPES_ZFS
                or self.type_name in TYPES_MBR_ZFS.values())

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "start": self.start, "size": self.size,
                "type_id": self.type_id, "type_name": self.type_name,
                "name": self.name, "likely_zfs": self.likely_zfs}

    def __str__(self) -> str:
        go = self.size / (1 << 30)
        nom = f" « {self.name} »" if self.name else ""
        return (f"partition {self.index} : offset {self.start}, {go:.2f} Gio, "
                f"{self.type_name}{nom}")


@dataclass
class PartitionTable:
    kind: str | None = None            # "gpt", "mbr" ou None
    sector_size: int | None = None
    partitions: list[Partition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def zfs_candidates(self) -> list[Partition]:
        """Partitions ZFS d'abord, puis les autres en secours."""
        zfs = [p for p in self.partitions if p.likely_zfs]
        autres = [p for p in self.partitions
                  if not p.likely_zfs and p.size > (16 << 20)]
        return zfs + autres

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "sector_size": self.sector_size,
                "partitions": [p.as_dict() for p in self.partitions],
                "errors": self.errors}


def _guid(brut: bytes) -> str:
    """GUID GPT : les trois premiers champs sont en petit-boutiste."""
    return str(uuid.UUID(bytes_le=brut))


def _nom_type(guid: str) -> str:
    return TYPES_ZFS.get(guid) or AUTRES_TYPES.get(guid) or "inconnu"


def _lire_gpt(lecteur, taille_support: int, secteur: int) -> PartitionTable | None:
    try:
        entete = lecteur(secteur, 92)
    except Exception:
        return None
    if entete[:8] != GPT_SIGNATURE:
        return None

    t = PartitionTable(kind="gpt", sector_size=secteur)
    (_sig, _rev, taille_entete, crc_entete, _res, _lba_courant, _lba_secours,
     _premier, _dernier) = struct.unpack_from("<8sIIIIQQQQ", entete, 0)
    lba_entrees, nb_entrees, taille_entree, crc_entrees = struct.unpack_from(
        "<QIII", entete, 72)

    # Controle d'integrite de l'en-tete (UEFI 2.10, 5.3.2) : le champ CRC est
    # mis a zero pour le calcul. Une erreur est signalee, jamais masquee, mais
    # n'empeche pas de tenter la lecture.
    if 92 <= taille_entete <= secteur:
        try:
            complet = bytearray(lecteur(secteur, taille_entete))
            struct.pack_into("<I", complet, 16, 0)
            if binascii.crc32(bytes(complet)) & 0xFFFFFFFF != crc_entete:
                t.errors.append("CRC de l'en-tete GPT incorrect")
        except Exception:
            pass

    if not (0 < nb_entrees <= 1024) or taille_entree < 128:
        t.errors.append(f"en-tete GPT incoherent : {nb_entrees} entrees de "
                        f"{taille_entree} octets")
        return t

    try:
        table = lecteur(lba_entrees * secteur, nb_entrees * taille_entree)
    except Exception as exc:
        t.errors.append(f"table des partitions illisible : {exc}")
        return t
    if binascii.crc32(table) & 0xFFFFFFFF != crc_entrees:
        t.errors.append("CRC de la table des partitions incorrect")

    for i in range(nb_entrees):
        e = table[i * taille_entree:(i + 1) * taille_entree]
        if len(e) < 128 or e[:16] == b"\x00" * 16:
            continue
        type_guid = _guid(e[0:16])
        premier_lba, dernier_lba = struct.unpack_from("<QQ", e, 32)
        nom = e[56:128].decode("utf-16-le", "replace").split("\x00", 1)[0]
        debut = premier_lba * secteur
        fin = (dernier_lba + 1) * secteur
        if debut >= taille_support or fin <= debut:
            continue
        t.partitions.append(Partition(
            index=i + 1, start=debut, size=min(fin, taille_support) - debut,
            type_id=type_guid, type_name=_nom_type(type_guid), name=nom))
    return t


def _lire_mbr(lecteur, taille_support: int, secteur: int = 512
              ) -> PartitionTable | None:
    try:
        mbr = lecteur(0, 512)
    except Exception:
        return None
    if mbr[510:512] != b"\x55\xaa":
        return None
    t = PartitionTable(kind="mbr", sector_size=secteur)
    for i in range(4):
        e = mbr[446 + i * 16:446 + (i + 1) * 16]
        type_code = e[4]
        if type_code in (0x00, 0xEE):        # vide ou MBR de protection GPT
            continue
        debut_lba, nb_secteurs = struct.unpack_from("<II", e, 8)
        if debut_lba == 0 or nb_secteurs == 0:
            continue
        debut = debut_lba * secteur
        if debut >= taille_support:
            continue
        t.partitions.append(Partition(
            index=i + 1, start=debut,
            size=min(nb_secteurs * secteur, taille_support - debut),
            type_id=f"0x{type_code:02x}",
            type_name=TYPES_MBR_ZFS.get(type_code, f"type MBR 0x{type_code:02x}")))
    return t if t.partitions else None


def read_partition_table(lecteur, taille_support: int) -> PartitionTable:
    """
    `lecteur(offset, longueur) -> bytes` lit le support brut.
    Essaie GPT en 512 puis 4096 octets par secteur, sinon MBR.
    """
    for secteur in TAILLES_SECTEUR:
        t = _lire_gpt(lecteur, taille_support, secteur)
        if t is not None and (t.partitions or t.errors):
            return t
    t = _lire_mbr(lecteur, taille_support)
    return t if t is not None else PartitionTable()
