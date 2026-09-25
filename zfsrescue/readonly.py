"""
Backend de lecture strictement en LECTURE SEULE.

Regle absolue du projet : aucune ecriture, jamais, sur une source.
Garanties apportees par ce module :
  * ouverture avec os.O_RDONLY uniquement (aucun O_WRONLY / O_RDWR / O_CREAT) ;
  * O_NOATIME demande lorsque c'est permis, pour ne meme pas toucher l'atime ;
  * lectures par os.pread() : aucun curseur partage, aucun effet de bord ;
  * aucune methode d'ecriture n'est exposee par la classe.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass

from . import consts


class ReadOnlyError(Exception):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    real_path: str
    size: int              # taille de la FENETRE exploitee (= support entier par defaut)
    psize: int             # taille alignee sur 256 Kio (vdev_psize d'OpenZFS)
    offset: int            # debut de la fenetre sur le support (0 = support entier)
    media_size: int        # taille totale du support
    kind: str              # "file" | "blockdev"
    logical_sector_size: int | None
    physical_sector_size: int | None
    noatime: bool

    @property
    def windowed(self) -> bool:
        """Vrai si l'on ne lit qu'une partie du support (une partition)."""
        return self.offset != 0 or self.size != self.media_size


class ReadOnlyDevice:
    """Image disque ou peripherique bloc ouvert en lecture seule."""

    def __init__(self, path: str, offset: int = 0, length: int | None = None):
        """
        `offset` et `length` permettent de ne lire qu'une FENETRE du support :
        c'est ainsi qu'on traite un vdev place dans une partition. Toutes les
        lectures ulterieures sont relatives a cette fenetre, si bien que les
        couches superieures (labels, mapping RAIDZ) n'ont pas a s'en soucier.
        """
        self.path = path
        self._real = os.path.realpath(path)

        st = os.stat(self._real)
        if stat.S_ISBLK(st.st_mode):
            kind = "blockdev"
        elif stat.S_ISREG(st.st_mode):
            kind = "file"
        else:
            raise ReadOnlyError(
                f"{path}: ni fichier regulier ni peripherique bloc "
                "(refus par securite)"
            )

        flags = os.O_RDONLY
        noatime = False
        if hasattr(os, "O_NOATIME"):
            try:
                self._fd = os.open(self._real, flags | os.O_NOATIME)
                noatime = True
            except PermissionError:
                self._fd = os.open(self._real, flags)
        else:
            self._fd = os.open(self._real, flags)

        # Taille : st_size pour un fichier, seek(END) pour un peripherique bloc.
        media = st.st_size if kind == "file" else os.lseek(self._fd, 0, os.SEEK_END)
        if media == 0:
            os.close(self._fd)
            raise ReadOnlyError(f"{path}: taille nulle")

        if offset < 0 or offset >= media:
            os.close(self._fd)
            raise ReadOnlyError(
                f"{path}: offset {offset} hors du support ({media} octets)")
        size = media - offset if length is None else min(length, media - offset)
        if size <= 0:
            os.close(self._fd)
            raise ReadOnlyError(f"{path}: fenetre de taille nulle")
        self._offset = offset

        # module/zfs/vdev.c:2229 -> osize = P2ALIGN(osize, sizeof(vdev_label_t))
        psize = size - (size % consts.VDEV_LABEL_SIZE)

        self.info = DeviceInfo(
            path=path,
            real_path=self._real,
            size=size,
            offset=offset,
            media_size=media,
            psize=psize,
            kind=kind,
            logical_sector_size=self._sysfs_int("queue/logical_block_size", kind),
            physical_sector_size=self._sysfs_int("queue/physical_block_size", kind),
            noatime=noatime,
        )

    # -- lecture ------------------------------------------------------------
    def pread(self, offset: int, length: int) -> bytes:
        """Lit exactement `length` octets a `offset`. Leve si tronque."""
        if offset < 0 or length < 0:
            raise ReadOnlyError("offset/longueur negatif")
        if offset + length > self.info.size:
            raise ReadOnlyError(
                f"lecture hors support : {offset}+{length} > {self.info.size}"
            )
        out = bytearray()
        base = self._offset + offset
        while len(out) < length:
            chunk = os.pread(self._fd, length - len(out), base + len(out))
            if not chunk:
                raise ReadOnlyError(
                    f"lecture courte a l'offset {offset + len(out)} "
                    f"({len(out)}/{length} octets)"
                )
            out += chunk
        return bytes(out)

    def sha256(self, chunk_size: int = 8 << 20) -> str:
        h = hashlib.sha256()
        off = 0
        while off < self.info.size:
            n = min(chunk_size, self.info.size - off)
            h.update(self.pread(off, n))
            off += n
        return h.hexdigest()

    # -- divers -------------------------------------------------------------
    def _sysfs_int(self, rel: str, kind: str) -> int | None:
        if kind != "blockdev":
            return None
        name = os.path.basename(self._real)
        for cand in (f"/sys/block/{name}/{rel}",
                     f"/sys/class/block/{name}/{rel}"):
            try:
                with open(cand) as fh:
                    return int(fh.read().strip())
            except OSError:
                continue
        return None

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ReadOnlyDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        fenetre = (f" offset={self.info.offset}" if self.info.windowed else "")
        return f"<ReadOnlyDevice {self.path}{fenetre} size={self.info.size}>"
