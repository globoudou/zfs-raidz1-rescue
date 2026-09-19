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
    size: int              # taille brute constatee
    psize: int             # taille alignee sur 256 Kio (vdev_psize d'OpenZFS)
    kind: str              # "file" | "blockdev"
    logical_sector_size: int | None
    physical_sector_size: int | None
    noatime: bool


class ReadOnlyDevice:
    """Image disque ou peripherique bloc ouvert en lecture seule."""

    def __init__(self, path: str):
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
        size = st.st_size if kind == "file" else os.lseek(self._fd, 0, os.SEEK_END)
        if size == 0:
            os.close(self._fd)
            raise ReadOnlyError(f"{path}: taille nulle")

        # module/zfs/vdev.c:2229 -> osize = P2ALIGN(osize, sizeof(vdev_label_t))
        psize = size - (size % consts.VDEV_LABEL_SIZE)

        self.info = DeviceInfo(
            path=path,
            real_path=self._real,
            size=size,
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
        while len(out) < length:
            chunk = os.pread(self._fd, length - len(out), offset + len(out))
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
        return f"<ReadOnlyDevice {self.path} size={self.info.size}>"
