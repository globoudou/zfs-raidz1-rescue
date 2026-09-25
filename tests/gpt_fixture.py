"""
Fabrique des images de test munies d'une table de partitions.

Sert a verifier que l'outil retrouve un vdev place dans une PARTITION, comme
le font FreeBSD, FreeNAS/TrueNAS et la plupart des installations Linux.
"""

from __future__ import annotations

import binascii
import os
import struct
import uuid

SECTEUR = 512
FREEBSD_SWAP = uuid.UUID("516e7cb5-6ecf-11d6-8ff8-00022d09712b")
FREEBSD_ZFS = uuid.UUID("516e7cba-6ecf-11d6-8ff8-00022d09712b")


def _entree(type_guid: uuid.UUID, premier_lba: int, dernier_lba: int,
            nom: str) -> bytes:
    e = bytearray(128)
    e[0:16] = type_guid.bytes_le
    e[16:32] = uuid.uuid4().bytes_le
    struct.pack_into("<QQQ", e, 32, premier_lba, dernier_lba, 0)
    encode = nom.encode("utf-16-le")[:72]
    e[56:56 + len(encode)] = encode
    return bytes(e)


def creer_image_gpt(chemin: str, partitions: list[tuple[uuid.UUID, int, str]],
                    remplissage: dict[int, bytes] | None = None) -> dict[int, int]:
    """
    `partitions` : liste de (type_guid, taille en octets, nom).
    `remplissage` : index de partition (1..n) -> contenu a y ecrire.
    Retourne {index: offset en octets}.
    """
    nb_entrees, taille_entree = 128, 128
    secteurs_table = (nb_entrees * taille_entree + SECTEUR - 1) // SECTEUR
    premier_utilisable = 2 + secteurs_table
    # aligne sur 1 Mio, comme le font les outils de partitionnement
    courant = max(premier_utilisable, (1 << 20) // SECTEUR)

    entrees, offsets = [], {}
    for i, (type_guid, taille, nom) in enumerate(partitions, start=1):
        nb = taille // SECTEUR
        entrees.append(_entree(type_guid, courant, courant + nb - 1, nom))
        offsets[i] = courant * SECTEUR
        courant += nb
    table = b"".join(entrees).ljust(nb_entrees * taille_entree, b"\x00")

    dernier_utilisable = courant - 1
    total_secteurs = courant + secteurs_table + 1

    def entete(lba_courant: int, lba_secours: int, lba_entrees: int) -> bytes:
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<II", h, 8, 0x00010000, 92)
        struct.pack_into("<I", h, 16, 0)                      # CRC, calcule apres
        struct.pack_into("<QQQQ", h, 24, lba_courant, lba_secours,
                         premier_utilisable, dernier_utilisable)
        h[56:72] = uuid.uuid4().bytes_le
        struct.pack_into("<QIII", h, 72, lba_entrees, nb_entrees, taille_entree,
                         binascii.crc32(table) & 0xFFFFFFFF)
        struct.pack_into("<I", h, 16, binascii.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h)

    with open(chemin, "wb") as f:
        f.truncate(total_secteurs * SECTEUR)
        # MBR de protection
        mbr = bytearray(512)
        mbr[446 + 4] = 0xEE
        struct.pack_into("<II", mbr, 446 + 8, 1, min(total_secteurs - 1, 0xFFFFFFFF))
        mbr[510:512] = b"\x55\xaa"
        f.seek(0); f.write(mbr)
        # en-tete principal + table
        f.seek(SECTEUR); f.write(entete(1, total_secteurs - 1, 2))
        f.seek(2 * SECTEUR); f.write(table)
        # copie de secours
        f.seek((total_secteurs - 1 - secteurs_table) * SECTEUR); f.write(table)
        f.seek((total_secteurs - 1) * SECTEUR)
        f.write(entete(total_secteurs - 1, 1, total_secteurs - 1 - secteurs_table))
        # contenu des partitions
        for index, donnees in (remplissage or {}).items():
            f.seek(offsets[index]); f.write(donnees)
    return offsets


def copier_dans_partition(chemin_image: str, offset: int, source: str) -> None:
    """Copie un fichier dans l'image a l'offset donne, par blocs."""
    with open(source, "rb") as src, open(chemin_image, "r+b") as dst:
        dst.seek(offset)
        while True:
            bloc = src.read(1 << 20)
            if not bloc:
                break
            dst.write(bloc)


def copier_labels(chemin_image: str, offset_partition: int, source: str,
                  taille_source: int) -> None:
    """
    Copie uniquement les quatre zones de label d'une image ZFS dans une
    partition, en laissant le reste creux. Suffit a tester la detection de
    partition sans dupliquer des centaines de mega-octets.
    """
    taille_label = 256 * 1024
    psize = taille_source - (taille_source % taille_label)
    zones = [0, taille_label, psize - 2 * taille_label, psize - taille_label]
    with open(source, "rb") as src, open(chemin_image, "r+b") as dst:
        for z in zones:
            src.seek(z)
            dst.seek(offset_partition + z)
            dst.write(src.read(taille_label))
