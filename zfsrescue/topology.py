"""
Decouverte du pool et de la topologie vdev a partir des labels disponibles.

Les vdev absents sont representes explicitement par l'etat MISSING : l'outil
ne suppose jamais leur contenu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import consts
from .label import VdevLabel, read_all_labels, uberblock_shift
from .parttable import Partition, PartitionTable, read_partition_table
from .readonly import ReadOnlyDevice

STATE_AVAILABLE = "AVAILABLE"
STATE_MISSING = "MISSING"


@dataclass
class ScannedDevice:
    """Un support fourni par l'utilisateur, avec ses 4 labels."""
    device: ReadOnlyDevice
    labels: list[VdevLabel]
    partition: Partition | None = None          # si le vdev est dans une partition
    partition_table: PartitionTable | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def valid_labels(self) -> list[VdevLabel]:
        return [l for l in self.labels if l.valid]

    @property
    def best_label(self) -> VdevLabel | None:
        v = self.valid_labels
        return max(v, key=lambda l: l.txg or 0) if v else None

    @property
    def guid(self) -> int | None:
        b = self.best_label
        return b.config.get("guid") if b else None

    @property
    def pool_guid(self) -> int | None:
        b = self.best_label
        return b.config.get("pool_guid") if b else None

    def as_dict(self) -> dict[str, Any]:
        i = self.device.info
        return {
            "path": i.path,
            "real_path": i.real_path,
            "kind": i.kind,
            "media_size": i.media_size,
            "window_offset": i.offset,
            "partition": self.partition.as_dict() if self.partition else None,
            "partition_table": (self.partition_table.as_dict()
                                if self.partition_table else None),
            "notes": self.notes,
            "size": i.size,
            "psize": i.psize,
            "size_aligned_off": i.size - i.psize,
            "logical_sector_size": i.logical_sector_size,
            "physical_sector_size": i.physical_sector_size,
            "opened_with_noatime": i.noatime,
            "vdev_guid": self.guid,
            "pool_guid": self.pool_guid,
            "labels_valid": len(self.valid_labels),
            "labels": [l.as_dict() for l in self.labels],
        }


@dataclass
class LeafVdev:
    id: int
    guid: int | None
    type: str
    label_path: str | None
    state: str
    device_path: str | None = None
    psize: int | None = None
    asize: int | None = None
    labels_valid: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "guid": self.guid, "type": self.type,
            "path_in_label": self.label_path, "state": self.state,
            "device_path": self.device_path, "psize": self.psize,
            "asize": self.asize, "labels_valid": self.labels_valid,
        }


@dataclass
class TopLevelVdev:
    id: int
    guid: int | None
    type: str
    nparity: int | None
    ashift: int | None
    asize: int | None
    metaslab_array: int | None
    metaslab_shift: int | None
    is_log: int | None
    children: list[LeafVdev] = field(default_factory=list)

    @property
    def ncols(self) -> int:
        return len(self.children)

    @property
    def n_available(self) -> int:
        return sum(1 for c in self.children if c.state == STATE_AVAILABLE)

    @property
    def n_missing(self) -> int:
        return sum(1 for c in self.children if c.state == STATE_MISSING)

    def raidz_mapping_params(self) -> dict[str, Any] | None:
        if self.type != "raidz" or self.ashift is None:
            return None
        sector = 1 << self.ashift
        per_child = (self.asize // self.ncols) if (self.asize and self.ncols) else None
        return {
            "vdev_type": self.type,
            "nparity": self.nparity,
            "ncols": self.ncols,
            "ashift": self.ashift,
            "sector_size": sector,
            "asize_total": self.asize,
            "asize_per_child": per_child,
            # Les donnees commencent apres L0+L1+zone de boot (4 Mio) sur CHAQUE
            # feuille ; les offsets DVA sont relatifs a ce debut de zone allouable.
            "leaf_data_start": consts.VDEV_LABEL_START_SIZE,
            "leaf_reserved_end": consts.VDEV_LABEL_END_SIZE,
            "metaslab_array": self.metaslab_array,
            "metaslab_shift": self.metaslab_shift,
            "metaslab_size": (1 << self.metaslab_shift) if self.metaslab_shift else None,
            "metaslab_count": ((self.asize >> self.metaslab_shift)
                               if (self.asize and self.metaslab_shift) else None),
            "uberblock_shift": uberblock_shift(self.ashift),
            "uberblock_size": 1 << uberblock_shift(self.ashift),
            "uberblocks_per_label": consts.VDEV_UBERBLOCK_RING >> uberblock_shift(self.ashift),
            "max_reconstructible_missing_columns": self.nparity,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "guid": self.guid, "type": self.type,
            "nparity": self.nparity, "ashift": self.ashift, "asize": self.asize,
            "is_log": self.is_log,
            "children_total": self.ncols,
            "children_available": self.n_available,
            "children_missing": self.n_missing,
            "children": [c.as_dict() for c in self.children],
            "raidz_mapping": self.raidz_mapping_params(),
        }


@dataclass
class PoolTopology:
    name: str | None = None
    pool_guid: int | None = None
    state: int | None = None
    version: int | None = None
    txg: int | None = None
    hostid: int | None = None
    hostname: str | None = None
    errata: int | None = None
    source_device: str | None = None
    source_label: int | None = None
    top_levels: list[TopLevelVdev] = field(default_factory=list)
    features_for_read: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    inconsistencies: list[str] = field(default_factory=list)

    @property
    def state_name(self) -> str:
        return consts.POOL_STATE_NAMES.get(self.state, f"inconnu({self.state})")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pool_guid": self.pool_guid,
            "state": self.state,
            "state_name": self.state_name,
            "version": self.version,
            "txg_from_label": self.txg,
            "hostid": self.hostid,
            "hostname": self.hostname,
            "errata": self.errata,
            "config_source": {"device": self.source_device, "label": self.source_label},
            "features_for_read": self.features_for_read,
            "top_level_vdevs": [t.as_dict() for t in self.top_levels],
            "warnings": self.warnings,
            "inconsistencies": self.inconsistencies,
        }


def scan_devices(paths: list[str], scan_partitions: bool = True,
                 offsets: dict[str, int] | None = None) -> list[ScannedDevice]:
    """
    Ouvre chaque support en lecture seule et y cherche les labels ZFS.

    Un vdev n'occupe pas toujours le support entier : FreeBSD, FreeNAS/TrueNAS
    et beaucoup d'installations Linux placent le pool dans une PARTITION, dont
    les labels se trouvent au debut et a la fin de la partition. Quand aucun
    label n'est trouve sur le support entier, on lit donc sa table de
    partitions et on essaie les partitions candidates. Rien n'est suppose :
    une partition n'est retenue que si de vrais labels valides s'y trouvent.
    """
    offsets = offsets or {}
    out = []
    for chemin in paths:
        if chemin in offsets:
            dev = ReadOnlyDevice(chemin, offset=offsets[chemin])
            sd = ScannedDevice(device=dev, labels=read_all_labels(dev))
            sd.notes.append(f"lecture forcee a partir de l'offset {offsets[chemin]}")
            out.append(sd)
            continue

        dev = ReadOnlyDevice(chemin)
        labels = read_all_labels(dev)
        sd = ScannedDevice(device=dev, labels=labels)

        if not sd.valid_labels and scan_partitions:
            table = read_partition_table(dev.pread, dev.info.size)
            sd.partition_table = table
            if table.kind:
                sd.notes.append(
                    f"aucun label sur le support entier ; table {table.kind.upper()} "
                    f"detectee ({len(table.partitions)} partition(s))")
                trouve = None
                for part in table.zfs_candidates:
                    try:
                        essai = ReadOnlyDevice(chemin, offset=part.start,
                                               length=part.size)
                    except Exception as exc:
                        sd.notes.append(f"partition {part.index} illisible : {exc}")
                        continue
                    labels_part = read_all_labels(essai)
                    if any(l.valid for l in labels_part):
                        trouve = (part, essai, labels_part)
                        break
                    essai.close()
                if trouve is not None:
                    part, essai, labels_part = trouve
                    dev.close()
                    sd = ScannedDevice(device=essai, labels=labels_part,
                                       partition=part, partition_table=table)
                    sd.notes.append(f"labels ZFS trouves dans la {part}")
                else:
                    sd.notes.append(
                        "aucune partition ne porte de label ZFS valide")
        out.append(sd)
    return out


def _leaf_from_config(cfg: dict[str, Any], idx: int,
                      by_guid: dict[int, ScannedDevice]) -> LeafVdev:
    guid = cfg.get("guid")
    leaf = LeafVdev(
        id=cfg.get("id", idx),
        guid=guid,
        type=cfg.get("type", "?"),
        label_path=cfg.get("path"),
        state=STATE_MISSING,
    )
    sd = by_guid.get(guid) if guid is not None else None
    if sd is not None:
        leaf.state = STATE_AVAILABLE
        leaf.device_path = sd.device.info.path
        leaf.psize = sd.device.info.psize
        leaf.asize = (sd.device.info.psize
                      - consts.VDEV_LABEL_START_SIZE - consts.VDEV_LABEL_END_SIZE)
        leaf.labels_valid = len(sd.valid_labels)
    return leaf


def build_topology(scanned: list[ScannedDevice]) -> PoolTopology:
    topo = PoolTopology()

    usable = [s for s in scanned if s.best_label is not None]
    for s in scanned:
        if s.best_label is None:
            topo.inconsistencies.append(
                f"{s.device.info.path} : aucun label ZFS valide "
                f"({len(s.labels)} emplacements examines)")
    if not usable:
        topo.warnings.append("aucun label exploitable : pool non identifiable")
        return topo

    # --- coherence entre supports --------------------------------------------
    pool_guids = {s.pool_guid for s in usable}
    if len(pool_guids) > 1:
        topo.inconsistencies.append(
            f"pool_guid differents entre supports : {sorted(pool_guids)}")

    guids = [s.guid for s in usable]
    if len(set(guids)) != len(guids):
        topo.inconsistencies.append(
            "deux supports portent le meme vdev guid (image dupliquee ?)")

    txgs = {s.device.info.path: s.best_label.txg for s in usable}
    if len(set(txgs.values())) > 1:
        topo.inconsistencies.append(
            "txg different selon les supports : " +
            ", ".join(f"{k}={v}" for k, v in txgs.items()))

    for s in usable:
        missing = [l.index for l in s.labels if not l.valid]
        if missing:
            topo.inconsistencies.append(
                f"{s.device.info.path} : label(s) invalide(s) {missing}")

    # --- configuration retenue : txg le plus eleve ----------------------------
    ref = max(usable, key=lambda s: s.best_label.txg or 0)
    ref_label = ref.best_label
    cfg = ref_label.config
    topo.source_device = ref.device.info.path
    topo.source_label = ref_label.index

    topo.name = cfg.get("name")
    topo.pool_guid = cfg.get("pool_guid")
    topo.state = cfg.get("state")
    topo.version = cfg.get("version")
    topo.txg = cfg.get("txg")
    topo.hostid = cfg.get("hostid")
    topo.hostname = cfg.get("hostname")
    topo.errata = cfg.get("errata")
    topo.features_for_read = cfg.get("features_for_read", {}) or {}

    by_guid = {s.guid: s for s in usable if s.guid is not None}

    # Le label d'une feuille ne decrit que SON vdev de premier niveau
    # (vdev_tree = le top-level auquel elle appartient).
    tree = cfg.get("vdev_tree") or {}
    trees = [tree] if tree.get("type") != "root" else (tree.get("children") or [])

    for t in trees:
        tl = TopLevelVdev(
            id=t.get("id", 0), guid=t.get("guid"), type=t.get("type", "?"),
            nparity=t.get("nparity"), ashift=t.get("ashift"), asize=t.get("asize"),
            metaslab_array=t.get("metaslab_array"),
            metaslab_shift=t.get("metaslab_shift"), is_log=t.get("is_log"),
        )
        for i, c in enumerate(t.get("children") or []):
            tl.children.append(_leaf_from_config(c, i, by_guid))
        topo.top_levels.append(tl)

    # nombre de vdev de premier niveau annonce par le label
    declared = cfg.get("vdev_children")
    if declared is not None and declared != len(topo.top_levels):
        topo.warnings.append(
            f"le pool declare {declared} vdev(s) de premier niveau mais un seul "
            f"est decrit par ce label : {len(topo.top_levels)} reconstruit(s). "
            "Les labels ne decrivent que le vdev auquel appartient le support.")

    # --- controles de coherence arithmetique ---------------------------------
    for tl in topo.top_levels:
        for leaf in tl.children:
            if leaf.asize is None or tl.asize is None or not tl.ncols:
                continue
            attendu = tl.asize // tl.ncols
            if leaf.asize != attendu:
                topo.inconsistencies.append(
                    f"{leaf.device_path} : asize calcule {leaf.asize} != "
                    f"asize/ncols du vdev {attendu} (taille d'image differente ?)")

    # --- controles de perimetre (CLAUDE.md) ----------------------------------
    _scope_checks(topo)
    return topo


def _scope_checks(topo: PoolTopology) -> None:
    hors = []
    if len(topo.top_levels) != 1:
        hors.append(f"{len(topo.top_levels)} vdev(s) de premier niveau reconstruits "
                    "(perimetre initial : 1 seul vdev raidz1)")
    for tl in topo.top_levels:
        if tl.type != "raidz":
            hors.append(f"vdev {tl.id} de type '{tl.type}' (perimetre : raidz)")
        elif tl.nparity != 1:
            hors.append(f"vdev {tl.id} : nparity={tl.nparity} (perimetre : raidz1)")
        elif tl.ncols != 4:
            hors.append(f"vdev {tl.id} : {tl.ncols} colonnes (perimetre : 4)")
        if tl.is_log:
            hors.append(f"vdev {tl.id} : vdev de log (hors perimetre)")
    for feat in topo.features_for_read:
        if "encryption" in feat or "dedup" in feat:
            hors.append(f"fonctionnalite active hors perimetre : {feat}")
    for h in hors:
        topo.warnings.append("HORS PERIMETRE: " + h)


def recoverability_summary(topo: PoolTopology) -> dict[str, Any]:
    """
    Ce que la topologie permet de dire AVANT toute lecture de donnees.
    Aucune estimation optimiste : on ne declare recuperable que ce qui l'est
    mathematiquement.
    """
    out = []
    for tl in topo.top_levels:
        if tl.type != "raidz" or tl.nparity is None:
            continue
        miss = tl.n_missing
        out.append({
            "vdev_id": tl.id,
            "columns_total": tl.ncols,
            "columns_available": tl.n_available,
            "columns_missing": miss,
            "parity": tl.nparity,
            "full_stripe_reconstructible": miss <= tl.nparity,
            "comment": (
                "Toute stripe reste reconstructible (pertes <= parite)."
                if miss <= tl.nparity else
                f"{miss} colonnes absentes pour {tl.nparity} parite(s) : une stripe "
                "utilisant toutes les colonnes est IRRECUPERABLE. Restent "
                "exploitables les blocs assez petits pour tenir entierement dans "
                "les colonnes survivantes, ceux auxquels il ne manque qu'une "
                "colonne utile, et les copies multiples de metadonnees."
            ),
        })
    return {"per_top_level_vdev": out}
