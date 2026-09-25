"""Rapports lisibles par un humain + structure JSON."""

from __future__ import annotations

import datetime as _dt
import json
import platform
from typing import Any

from . import __version__, consts
from .topology import (STATE_AVAILABLE, PoolTopology, ScannedDevice,
                       recoverability_summary)


def build_report(scanned: list[ScannedDevice], topo: PoolTopology,
                 include_full_labels: bool = False,
                 hashes: dict[str, str] | None = None) -> dict[str, Any]:
    devices = []
    for s in scanned:
        d = s.as_dict()
        if not include_full_labels:
            for lab in d["labels"]:
                lab.pop("config", None)
        if hashes and s.device.info.path in hashes:
            d["sha256"] = hashes[s.device.info.path]
        devices.append(d)

    return {
        "tool": {
            "name": "zfsrescue",
            "version": __version__,
            "stage": "etape 2 — labels et topologie",
            "openzfs_reference": consts.OPENZFS_REFERENCE,
            "read_only": True,
        },
        "run": {
            "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "host": platform.node(),
            "python": platform.python_version(),
        },
        "devices_supplied": devices,
        "pool": topo.as_dict(),
        "recoverability": recoverability_summary(topo),
    }


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(n) < 1024 or unit == "Tio":
            return f"{n:,.0f} {unit}".replace(",", " ") if unit == "o" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def render_text(report: dict[str, Any]) -> str:
    L: list[str] = []
    add = L.append

    add("=" * 78)
    add("  zfsrescue — analyse de labels ZFS (LECTURE SEULE)")
    add(f"  {report['run']['timestamp_utc']}  |  reference : "
        f"{report['tool']['openzfs_reference']}")
    add("=" * 78)

    add("")
    add("SUPPORTS FOURNIS")
    for d in report["devices_supplied"]:
        add(f"  {d['path']}")
        if d.get("partition"):
            p = d["partition"]
            add(f"      VDEV DANS UNE PARTITION : n°{p['index']} a l'offset "
                f"{p['start']} ({p['type_name']}"
                + (f" « {p['name']} »" if p.get("name") else "") + ")")
            add(f"      support entier : {d['media_size']} octets "
                f"({_fmt_size(d['media_size'])})")
        add(f"      type={d['kind']}  taille={d['size']} octets "
            f"({_fmt_size(d['size'])})  psize={d['psize']}")
        if d["size"] != d["psize"]:
            add(f"      note : {d['size_aligned_off']} octets ignores par "
                "l'alignement 256 Kio (comportement OpenZFS)")
        if d["logical_sector_size"]:
            add(f"      secteurs : logique={d['logical_sector_size']} "
                f"physique={d['physical_sector_size']}")
        add(f"      vdev_guid={d['vdev_guid']}  pool_guid={d['pool_guid']}")
        add(f"      labels valides : {d['labels_valid']}/4  "
            f"(O_NOATIME={'oui' if d['opened_with_noatime'] else 'non'})")
        for lab in d["labels"]:
            ck = lab["checksum"]
            etat = "OK " if lab["valid"] else "KO "
            det = ("checksum SHA-256 verifie" if ck and ck["valid"]
                   else (", ".join(lab["errors"]) or "checksum invalide"))
            add(f"        L{lab['index']} @ {lab['offset']:>12}  {etat} {det}")
        for n in d.get("notes", []):
            add(f"      note : {n}")
        table = d.get("partition_table")
        if table and table.get("kind") and not d.get("partition"):
            add(f"      table de partitions {table['kind'].upper()} "
                f"(secteur {table['sector_size']}) :")
            for p in table["partitions"]:
                marque = "  <-- candidate ZFS" if p["likely_zfs"] else ""
                add(f"        n°{p['index']:<2} offset {p['start']:>14}  "
                    f"{_fmt_size(p['size']):>10}  {p['type_name']}{marque}")
            for e in table.get("errors", []):
                add(f"        ! {e}")
        if "sha256" in d:
            add(f"      sha256 image : {d['sha256']}")

    p = report["pool"]
    add("")
    add("POOL IDENTIFIE")
    add(f"  nom           : {p['name']}")
    add(f"  pool_guid     : {p['pool_guid']}")
    add(f"  etat          : {p['state']} ({p['state_name']})")
    add(f"  version       : {p['version']}"
        + (" (feature flags)" if p["version"] == 5000 else ""))
    add(f"  txg (label)   : {p['txg_from_label']}")
    add(f"  hote d'origine: {p['hostname']} (hostid={p['hostid']})")
    add(f"  errata        : {p['errata']}")
    add(f"  config issue  : {p['config_source']['device']} label "
        f"L{p['config_source']['label']}")
    if p["features_for_read"]:
        add("  features_for_read :")
        for k in sorted(p["features_for_read"]):
            add(f"      - {k}")

    add("")
    add("TOPOLOGIE")
    for tl in p["top_level_vdevs"]:
        add(f"  vdev {tl['id']} : {tl['type']}{tl['nparity'] or ''}  "
            f"ashift={tl['ashift']}  asize={tl['asize']} ({_fmt_size(tl['asize'])})")
        add(f"      colonnes : {tl['children_total']} "
            f"({tl['children_available']} disponibles, {tl['children_missing']} MISSING)")
        for c in tl["children"]:
            mark = "  " if c["state"] == STATE_AVAILABLE else ">>"
            src = c["device_path"] or "(aucune image fournie)"
            add(f"      {mark} col[{c['id']}] {c['state']:<9} guid={c['guid']}")
            add(f"           label_path : {c['path_in_label']}")
            add(f"           source     : {src}")
            if c["asize"] is not None:
                add(f"           asize      : {c['asize']} ({_fmt_size(c['asize'])})"
                    f"  labels valides : {c['labels_valid']}/4")

        m = tl["raidz_mapping"]
        if m:
            add("")
            add("      PARAMETRES DE MAPPING RAIDZ (etape 3)")
            add(f"        nparity                 : {m['nparity']}")
            add(f"        colonnes (ncols)        : {m['ncols']}")
            add(f"        ashift / secteur        : {m['ashift']} / {m['sector_size']} octets")
            add(f"        asize total / colonne   : {m['asize_total']} / {m['asize_per_child']}")
            add(f"        debut zone allouable    : {m['leaf_data_start']} octets "
                f"({_fmt_size(m['leaf_data_start'])}) sur chaque feuille")
            add(f"        reserve de fin          : {m['leaf_reserved_end']} octets")
            add(f"        metaslab : array={m['metaslab_array']} shift={m['metaslab_shift']} "
                f"taille={_fmt_size(m['metaslab_size'])} nombre={m['metaslab_count']}")
            add(f"        uberblocks : {m['uberblocks_per_label']} par label de "
                f"{m['uberblock_size']} octets (shift={m['uberblock_shift']})")

    add("")
    add("RECUPERABILITE THEORIQUE")
    for r in report["recoverability"]["per_top_level_vdev"]:
        add(f"  vdev {r['vdev_id']} : {r['columns_available']}/{r['columns_total']} "
            f"colonnes disponibles, parite={r['parity']}")
        add(f"  stripe complete reconstructible : "
            f"{'OUI' if r['full_stripe_reconstructible'] else 'NON'}")
        for line in _wrap(r["comment"], 72):
            add(f"      {line}")

    if p["warnings"]:
        add("")
        add("AVERTISSEMENTS")
        for w in p["warnings"]:
            add(f"  ! {w}")
    if p["inconsistencies"]:
        add("")
        add("INCOHERENCES DETECTEES")
        for w in p["inconsistencies"]:
            add(f"  ! {w}")

    add("")
    add("Aucune ecriture n'a ete effectuee : supports ouverts en O_RDONLY.")
    add("=" * 78)
    return "\n".join(L)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)
