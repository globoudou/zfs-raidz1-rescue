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

    # -- balayage independant de l'arborescence ----------------------------
    def scan(self, pool_name: str = "pool", probe: bool = True) -> DslScan:
        """
        Retrouve les datasets SANS suivre la hierarchie.

        Quand le ZAP des datasets enfants d'un repertoire DSL est perdu, la
        descente nominale s'arrete net alors que les objets `dsl_dataset` du
        MOS — qui portent chacun, dans leur bonus, le pointeur vers l'objset du
        dataset — restent parfaitement lisibles. On les enumere donc
        directement, puis on rattache les noms que les ZAP encore lisibles
        permettent de reconstituer. Les autres gardent un nom synthetique :
        leur contenu est accessible, seul leur nom d'origine est perdu.
        """
        scan = DslScan()
        repertoires: dict[int, DslDir] = {}
        datasets: dict[int, DslDataset] = {}

        for oid, dn, _etat in self.dmu.iter_dnodes(self.mos.meta_dnode):
            if dn is None or not dn.allocated or not dn.bonus:
                continue
            type_bonus = dn.bonustype_name
            if type_bonus == "dsl_dir":
                try:
                    repertoires[oid] = DslDir.from_bonus(oid, dn.bonus)
                except ValueError as exc:
                    scan.errors.append(f"dsl_dir {oid} : {exc}")
            elif type_bonus == "dsl_dataset":
                try:
                    datasets[oid] = DslDataset.from_bonus(oid, dn.bonus)
                except ValueError as exc:
                    scan.errors.append(f"dsl_dataset {oid} : {exc}")
        scan.dirs_found = len(repertoires)

        noms_dir = self._noms_des_repertoires(repertoires, pool_name, scan)

        # nom des datasets de tete, puis de leurs snapshots
        noms_ds: dict[int, str] = {}
        for obj_dir, d in repertoires.items():
            if d.head_dataset_obj and obj_dir in noms_dir:
                noms_ds[d.head_dataset_obj] = noms_dir[obj_dir]
        for obj_ds, nom in list(noms_ds.items()):
            ds = datasets.get(obj_ds)
            if ds is None or not ds.snapnames_zapobj:
                continue
            dn = self._dnode(ds.snapnames_zapobj)
            if dn is None:
                continue
            z = read_zap(self.dmu, dn)
            for e in z.entries:
                if e.values:
                    noms_ds.setdefault(e.values[0], f"{nom}@{e.name}")

        for oid in sorted(datasets):
            ds = datasets[oid]
            nom = noms_ds.get(oid)
            entree = DatasetEntry(
                object_id=oid, dataset=ds,
                name=nom or f"dataset_obj{oid}",
                named=nom is not None,
                kind="snapshot" if ds.is_snapshot else "systeme de fichiers",
                dir_obj=ds.dir_obj)
            if probe:
                self._sonder(entree)
            scan.entries.append(entree)

        scan.entries.sort(key=lambda e: (not e.named, e.name))
        return scan

    def _noms_des_repertoires(self, repertoires: dict[int, DslDir],
                              pool_name: str, scan: DslScan) -> dict[int, str]:
        """Descend depuis la racine tant que les ZAP d'enfants sont lisibles."""
        noms: dict[int, str] = {}
        try:
            racine = self.object_directory().get("root_dataset")
        except ValueError as exc:
            scan.errors.append(str(exc))
            return noms
        if racine is None:
            scan.errors.append("'root_dataset' absent de l'annuaire du MOS")
            return noms

        noms[int(racine)] = pool_name
        a_voir = [int(racine)]
        vus = set()
        while a_voir:
            obj = a_voir.pop()
            if obj in vus:
                continue
            vus.add(obj)
            d = repertoires.get(obj)
            if d is None or not d.child_dir_zapobj:
                continue
            dn = self._dnode(d.child_dir_zapobj)
            if dn is None:
                scan.child_maps_lost += 1
                continue
            z = read_zap(self.dmu, dn)
            if z.complete:
                scan.child_maps_read += 1
            else:
                scan.child_maps_lost += 1
            for e in z.entries:
                if not e.values:
                    continue
                enfant = e.values[0]
                noms[enfant] = f"{noms.get(obj, pool_name)}/{e.name}"
                a_voir.append(enfant)
        return noms

    def _sonder(self, entree: DatasetEntry) -> None:
        """Verifie si l'objset du dataset est lisible."""
        bp = entree.dataset.bp
        if bp is None or bp.is_hole:
            entree.objset_status = "TROU"
            return
        objset, res = self.dmu.read_objset(bp)
        entree.objset_status = res.status
        entree.objset = objset
        if objset is None and res.detail:
            entree.errors.append(res.detail)

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


# --- balayage : retrouver les datasets sans suivre l'arborescence ------------
@dataclass
class DatasetEntry:
    """Un dataset retrouve par balayage du MOS."""
    object_id: int
    dataset: DslDataset
    name: str
    named: bool                     # True si le nom vient des ZAP, False si synthetique
    kind: str                       # "systeme de fichiers" | "snapshot"
    dir_obj: int = 0
    objset_status: str = ""
    objset: Objset | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.objset is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id, "name": self.name, "named": self.named,
            "kind": self.kind, "dir_obj": self.dir_obj,
            "objset_status": self.objset_status,
            "objset_type": self.objset.type_name if self.objset else None,
            "objset_bp": str(self.dataset.bp) if self.dataset.bp else None,
            "creation_txg": self.dataset.creation_txg,
            "creation_utc": self.dataset.creation_iso,
            "referenced_bytes": self.dataset.referenced_bytes,
            "guid": self.dataset.guid,
            "errors": self.errors,
        }


@dataclass
class DslScan:
    entries: list[DatasetEntry] = field(default_factory=list)
    dirs_found: int = 0
    child_maps_read: int = 0
    child_maps_lost: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[DatasetEntry]:
        return [e for e in self.entries if e.usable]

    def summary(self) -> dict[str, Any]:
        return {
            "datasets_found": len(self.entries),
            "with_readable_objset": len(self.usable),
            "named": sum(1 for e in self.entries if e.named),
            "snapshots": sum(1 for e in self.entries if e.kind == "snapshot"),
            "dsl_dirs_found": self.dirs_found,
            "child_maps_read": self.child_maps_read,
            "child_maps_lost": self.child_maps_lost,
            "errors": self.errors,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.summary(),
                "datasets": [e.as_dict() for e in self.entries]}
