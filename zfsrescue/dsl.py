"""
Couche DSL : la hierarchie des datasets, depuis le MOS.

Reference normative : OpenZFS 2.3.9
  * dsl_dir_phys_t      include/sys/dsl_dir.h:69-92    (bonus du dnode)
  * dsl_dataset_phys_t  include/sys/dsl_dataset.h:140-172 (bonus du dnode)
  * annuaire du MOS     l'objet 1 (ZAP) contient "root_dataset"

Chemin de navigation :

    uberblock.rootbp -> objset du MOS
        objet 1 (ZAP "object directory")
            "root_dataset" -> objet dsl_dir de la racine
                dd_head_dataset_obj  -> dsl_dataset du systeme de fichiers
                    ds_bp            -> objset du dataset (les fichiers !)
                dd_child_dir_zapobj  -> ZAP nom -> objet dsl_dir des enfants
                ds_snapnames_zapobj  -> ZAP nom -> objet dsl_dataset des snapshots
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from typing import Any

from .blkptr import BlockPointer, parse_blkptr
from .dmu import Dnode, DmuReader, Objset
from .zap import Zap, read_zap

MOS_OBJECT_DIRECTORY = 1


@dataclass
class DslDir:
    object_id: int
    creation_time: int
    head_dataset_obj: int
    parent_obj: int
    origin_obj: int
    child_dir_zapobj: int
    used_bytes: int
    compressed_bytes: int
    uncompressed_bytes: int
    quota: int
    reserved: int
    props_zapobj: int
    deleg_zapobj: int
    flags: int

    @classmethod
    def from_bonus(cls, object_id: int, bonus: bytes) -> "DslDir":
        if len(bonus) < 13 * 8:
            raise ValueError(f"bonus dsl_dir trop court : {len(bonus)}")
        v = struct.unpack_from("<13Q", bonus, 0)
        return cls(object_id, *v)

    def as_dict(self) -> dict[str, Any]:
        return {"object_id": self.object_id,
                "head_dataset_obj": self.head_dataset_obj,
                "parent_obj": self.parent_obj, "origin_obj": self.origin_obj,
                "child_dir_zapobj": self.child_dir_zapobj,
                "props_zapobj": self.props_zapobj,
                "used_bytes": self.used_bytes}


@dataclass
class DslDataset:
    object_id: int
    dir_obj: int
    prev_snap_obj: int
    prev_snap_txg: int
    next_snap_obj: int
    snapnames_zapobj: int
    num_children: int
    creation_time: int
    creation_txg: int
    deadlist_obj: int
    referenced_bytes: int
    compressed_bytes: int
    uncompressed_bytes: int
    unique_bytes: int
    fsid_guid: int
    guid: int
    flags: int
    bp: BlockPointer | None = None

    @property
    def is_snapshot(self) -> bool:
        return self.num_children != 0 or self.snapnames_zapobj == 0

    @property
    def creation_iso(self) -> str:
        if not self.creation_time:
            return ""
        return _dt.datetime.fromtimestamp(
            self.creation_time, _dt.timezone.utc).isoformat()

    @classmethod
    def from_bonus(cls, object_id: int, bonus: bytes) -> "DslDataset":
        # 16 champs de 64 bits, puis ds_bp (dsl_dataset.h:141-167)
        if len(bonus) < 16 * 8 + 128:
            raise ValueError(f"bonus dsl_dataset trop court : {len(bonus)}")
        v = struct.unpack_from("<16Q", bonus, 0)
        bp = parse_blkptr(bonus, 16 * 8)
        return cls(object_id, *v, bp=bp)

    def as_dict(self) -> dict[str, Any]:
        return {"object_id": self.object_id, "dir_obj": self.dir_obj,
                "guid": self.guid, "creation_txg": self.creation_txg,
                "creation_time": self.creation_time,
                "creation_utc": self.creation_iso,
                "referenced_bytes": self.referenced_bytes,
                "snapnames_zapobj": self.snapnames_zapobj,
                "prev_snap_obj": self.prev_snap_obj,
                "objset_bp": str(self.bp) if self.bp else None}


@dataclass
class DatasetNode:
    """Un dataset dans l'arborescence reconstruite."""
    name: str
    dir_obj: int
    dsl_dir: DslDir | None = None
    dataset: DslDataset | None = None
    snapshots: list[tuple[str, int]] = field(default_factory=list)
    children: list["DatasetNode"] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "dir_obj": self.dir_obj,
            "dsl_dir": self.dsl_dir.as_dict() if self.dsl_dir else None,
            "dataset": self.dataset.as_dict() if self.dataset else None,
            "snapshots": [{"name": n, "obj": o} for n, o in self.snapshots],
            "errors": self.errors,
            "children": [c.as_dict() for c in self.children],
        }

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class DslReader:
    """Navigation dans la hierarchie des datasets a partir du MOS."""

    def __init__(self, dmu: DmuReader, mos: Objset):
        self.dmu = dmu
        self.mos = mos

    def object_directory(self) -> Zap:
        dn, _etat = self.dmu.read_dnode(self.mos.meta_dnode,
                                        MOS_OBJECT_DIRECTORY)
        if dn is None or not dn.allocated:
            raise ValueError("annuaire d'objets du MOS illisible")
        return read_zap(self.dmu, dn)

    def _dnode(self, obj: int) -> Dnode | None:
        dn, _etat = self.dmu.read_dnode(self.mos.meta_dnode, obj)
        return dn if (dn is not None and dn.allocated) else None

    def read_dsl_dir(self, obj: int) -> DslDir | None:
        dn = self._dnode(obj)
        if dn is None or not dn.bonus:
            return None
        try:
            return DslDir.from_bonus(obj, dn.bonus)
        except ValueError:
            return None

    def read_dsl_dataset(self, obj: int) -> DslDataset | None:
        dn = self._dnode(obj)
        if dn is None or not dn.bonus:
            return None
        try:
            return DslDataset.from_bonus(obj, dn.bonus)
        except ValueError:
            return None

    def build_tree(self, pool_name: str = "pool") -> DatasetNode:
        od = self.object_directory()
        racine = od.get("root_dataset")
        if racine is None:
            raise ValueError("'root_dataset' absent de l'annuaire du MOS")
        return self._noeud(int(racine), pool_name)

    def _noeud(self, dir_obj: int, nom: str, profondeur: int = 0) -> DatasetNode:
        n = DatasetNode(name=nom, dir_obj=dir_obj)
        if profondeur > 32:
            n.errors.append("profondeur maximale atteinte")
            return n

        n.dsl_dir = self.read_dsl_dir(dir_obj)
        if n.dsl_dir is None:
            n.errors.append(f"dsl_dir {dir_obj} illisible")
            return n

        if n.dsl_dir.head_dataset_obj:
            n.dataset = self.read_dsl_dataset(n.dsl_dir.head_dataset_obj)
            if n.dataset is None:
                n.errors.append(
                    f"dsl_dataset {n.dsl_dir.head_dataset_obj} illisible")

        if n.dataset and n.dataset.snapnames_zapobj:
            dn = self._dnode(n.dataset.snapnames_zapobj)
            if dn is not None:
                z = read_zap(self.dmu, dn)
                n.snapshots = sorted((e.name, e.values[0]) for e in z.entries
                                     if e.values)
                if not z.complete:
                    n.errors.append("liste des snapshots incomplete")

        if n.dsl_dir.child_dir_zapobj:
            dn = self._dnode(n.dsl_dir.child_dir_zapobj)
            if dn is None:
                n.errors.append("ZAP des datasets enfants illisible")
            else:
                z = read_zap(self.dmu, dn)
                if not z.complete:
                    n.errors.append(
                        f"liste des datasets enfants incomplete "
                        f"({len(z.entries)} lus, {z.declared_entries} annonces)")
                for e in sorted(z.entries, key=lambda e: e.name):
                    if not e.values:
                        continue
                    n.children.append(
                        self._noeud(e.values[0], f"{nom}/{e.name}",
                                    profondeur + 1))
        return n
