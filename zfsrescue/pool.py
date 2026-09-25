"""
Assemblage de haut niveau : ouvrir un pool en lecture seule a partir des
supports disponibles, jusqu'au MOS et aux datasets.

Regroupe les etapes 2 a 6 : labels -> topologie -> uberblocks -> MOS -> DSL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .blockio import PoolReader
from .dmu import DmuReader, Objset
from .dsl import DatasetNode, DslReader
from .readonly import ReadOnlyDevice
from .topology import PoolTopology, ScannedDevice, build_topology, scan_devices
from .uberblock import Uberblock, UberblockSet, guid_sum_expected, read_uberblocks


class PoolOpenError(Exception):
    pass


@dataclass
class OpenedPool:
    scanned: list[ScannedDevice]
    topology: PoolTopology
    uberblocks: UberblockSet
    uberblock: Uberblock
    reader: PoolReader
    dmu: DmuReader
    mos: Objset
    warnings: list[str] = field(default_factory=list)

    @property
    def devices_by_column(self) -> dict[int, ReadOnlyDevice]:
        return self.reader.devices

    def dataset_tree(self) -> DatasetNode:
        return DslReader(self.dmu, self.mos).build_tree(
            self.topology.name or "pool")

    def close(self) -> None:
        for sd in self.scanned:
            sd.device.close()

    def summary(self) -> dict[str, Any]:
        tl = self.topology.top_levels[0]
        return {
            "pool": self.topology.name,
            "pool_guid": self.topology.pool_guid,
            "vdev": {"type": tl.type, "nparity": tl.nparity,
                     "columns_total": tl.ncols,
                     "columns_available": sorted(self.reader.devices),
                     "columns_missing": sorted(
                         c.id for c in tl.children
                         if c.id not in self.reader.devices),
                     "ashift": tl.ashift},
            "uberblock": {"txg": self.uberblock.txg,
                          "timestamp_utc": self.uberblock.timestamp_iso,
                          "rootbp": str(self.uberblock.rootbp)},
            "mos": {"type": self.mos.type_name,
                    "objects_declared": self.uberblock.rootbp.fill},
            "warnings": self.warnings,
        }


def open_pool(paths: list[str], txg: int | None = None,
              scan_partitions: bool = True,
              offset: int | None = None) -> OpenedPool:
    """
    Ouvre le pool en lecture seule.

    `txg`             choisit un etat historique precis (sinon le plus recent) ;
    `scan_partitions` cherche le vdev dans les partitions si le support entier
                      n'en porte pas (cas FreeBSD / FreeNAS / TrueNAS) ;
    `offset`          force le debut du vdev sur chaque support.
    """
    offsets = {c: offset for c in paths} if offset is not None else None
    scanned = scan_devices(paths, scan_partitions=scan_partitions,
                           offsets=offsets)
    topo = build_topology(scanned)
    avertissements: list[str] = list(topo.warnings)

    if not topo.top_levels:
        for sd in scanned:
            sd.device.close()
        raise PoolOpenError("aucune topologie exploitable dans les labels")
    tl = topo.top_levels[0]
    if tl.ashift is None or tl.nparity is None:
        for sd in scanned:
            sd.device.close()
        raise PoolOpenError("geometrie du vdev incomplete")

    devices: dict[int, ReadOnlyDevice] = {}
    for sd in scanned:
        for enfant in tl.children:
            if enfant.guid is not None and enfant.guid == sd.guid:
                devices[enfant.id] = sd.device

    jeu = UberblockSet()
    for sd in scanned:
        jeu.all += read_uberblocks(sd.device, tl.ashift)

    if txg is None:
        ub = jeu.best
    else:
        candidats = [u for u in jeu.valid if u.txg == txg]
        ub = candidats[0] if candidats else None
        if ub is None:
            for sd in scanned:
                sd.device.close()
            raise PoolOpenError(f"aucun uberblock valide pour le txg {txg}")
    if ub is None:
        for sd in scanned:
            sd.device.close()
        raise PoolOpenError("aucun uberblock valide")

    attendu = guid_sum_expected(
        topo.pool_guid or 0, [t.guid or 0 for t in topo.top_levels],
        [c.guid or 0 for t in topo.top_levels for c in t.children])
    if ub.guid_sum != attendu:
        avertissements.append(
            f"ub_guid_sum ({ub.guid_sum}) != somme des guid connus ({attendu}) : "
            "des vdev existent probablement en dehors de ce que decrivent les labels")
    if ub.raidz_expansion_active:
        avertissements.append(
            "HORS PERIMETRE: expansion RAIDZ detectee dans l'uberblock")

    lecteur = PoolReader(devices, tl.ashift, tl.ncols, tl.nparity)
    dmu = DmuReader(lecteur)
    mos, res = dmu.read_objset(ub.rootbp)
    if mos is None:
        for sd in scanned:
            sd.device.close()
        raise PoolOpenError(
            f"MOS illisible ({res.status}) : {res.detail or 'aucune copie exploitable'}")

    return OpenedPool(scanned=scanned, topology=topo, uberblocks=jeu,
                      uberblock=ub, reader=lecteur, dmu=dmu, mos=mos,
                      warnings=avertissements)
