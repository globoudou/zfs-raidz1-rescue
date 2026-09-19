"""
Oracle independant : sortie de `zdb`, l'outil officiel OpenZFS.

Utilise en LECTURE SEULE sur les images de test. `zdb -l` n'ecrit jamais.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

ZDB = shutil.which("zdb") or next(
    (p for p in ("/usr/sbin/zdb", "/sbin/zdb") if os.path.exists(p)), None)

available = ZDB is not None


def run_zdb_l(image: str) -> str:
    return subprocess.run([ZDB, "-l", image], capture_output=True, text=True,
                          check=True).stdout


def parse_indented(text: str) -> dict:
    """
    Convertit la sortie indentee de `zdb -l` (un seul label) en dictionnaire.
    Gere les blocs 'children[N]:' et 'vdev_tree:'.
    """
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("-"):
            continue
        if re.match(r"^\s*LABEL \d+\s*$", raw):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        m = re.match(r"^([\w.:\[\]]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        if val == "":
            node: dict = {}
            cm = re.match(r"^children\[(\d+)\]$", key)
            if cm:
                parent.setdefault("children", []).append(node)
            else:
                parent[key] = node
            stack.append((indent, node))
        else:
            if val.startswith("'") and val.endswith("'"):
                parent[key] = val[1:-1]
            elif re.fullmatch(r"-?\d+", val):
                parent[key] = int(val)
            else:
                parent[key] = val
    return root


def label_configs(image: str) -> list[dict]:
    """Retourne la configuration de chaque label present d'apres zdb -l."""
    out = []
    text = run_zdb_l(image)
    blocks = re.split(r"-{10,}\s*\n\s*LABEL (\d+)\s*\n-{10,}\s*\n", text)
    # blocks = [preambule, idx, corps, idx, corps, ...]
    for i in range(1, len(blocks) - 1, 2):
        out.append(parse_indented(blocks[i + 1]))
    return out


def run_zdb_lu(image: str) -> str:
    return subprocess.run([ZDB, "-lu", image], capture_output=True, text=True,
                          check=True).stdout


def uberblocks(image: str) -> list[dict]:
    """
    Uberblocks vus par `zdb -lu`. zdb fusionne les copies identiques des 4
    labels et indique les labels concernes par la ligne 'labels = ...'.
    """
    out = []
    courant = None
    for ligne in run_zdb_lu(image).splitlines():
        m = re.match(r"^\s*Uberblock\[(\d+)\]\s*$", ligne)
        if m:
            courant = {"slot": int(m.group(1)), "labels": []}
            out.append(courant)
            continue
        if courant is None:
            continue
        m = re.match(r"^\s*labels = ([\d ]+)$", ligne)
        if m:
            courant["labels"] = [int(x) for x in m.group(1).split()]
            continue
        m = re.match(r"^\s*(\w+) = (.+?)\s*$", ligne)
        if m:
            cle, val = m.group(1), m.group(2)
            if cle in ("txg", "guid_sum", "version", "mmp_delay",
                       "checkpoint_txg", "mmp_valid"):
                courant[cle] = int(val)
            elif cle == "timestamp":
                courant["timestamp"] = int(val.split()[0])
            elif cle == "magic":
                courant["magic"] = int(val, 16)
            elif cle == "bp":
                courant["bp"] = val
                mc = re.search(r"cksum=([0-9a-f:]+)", val)
                if mc:
                    courant["bp_cksum"] = [int(x, 16)
                                           for x in mc.group(1).split(":")]
                md = re.findall(r"DVA\[\d+\]=<(\d+):([0-9a-f]+):([0-9a-f]+)>", val)
                courant["bp_dvas"] = [(int(v), int(o, 16), int(a, 16))
                                      for v, o, a in md]
            continue
        m = re.match(r"^\s*raidz_reflow state=(\d+) off=(\d+)\s*$", ligne)
        if m:
            courant["reflow_state"] = int(m.group(1))
            courant["reflow_offset"] = int(m.group(2))
    return out


def run_zdb(args: list[str], images_dir: str) -> str:
    return subprocess.run([ZDB, "-e", "-p", images_dir] + args,
                          capture_output=True, text=True).stdout


def objects(dataset: str, images_dir: str, section: str | None = None
            ) -> dict[int, dict]:
    """
    Objets listes par `zdb -dddd <dataset>` :
        Object  lvl   iblk   dblk  dsize  dnsize  lsize   %full  type

    `section` limite la collecte a un dataset precis de la sortie ("mos" pour
    le MOS), car zdb liste plusieurs datasets d'affilee.
    """
    def taille(txt: str) -> int:
        mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if txt[-1] in mult:
            return int(float(txt[:-1]) * mult[txt[-1]])
        return int(float(txt))

    out: dict[int, dict] = {}
    courant = None
    dans_section = section is None
    for ligne in run_zdb(["-dddd", dataset], images_dir).splitlines():
        m = re.match(r"^Dataset (\S+) ", ligne)
        if m:
            dans_section = (section is None or m.group(1) == section)
            courant = None
            continue
        if not dans_section:
            continue
        m = re.match(r"^\s{4,}(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+"
                     r"(\S+)\s+([\d.]+)\s+(.+?)\s*$", ligne)
        if m:
            courant = {
                "id": int(m.group(1)), "levels": int(m.group(2)),
                "iblk": taille(m.group(3)), "dblk": taille(m.group(4)),
                "dnsize": int(m.group(6)), "lsize": taille(m.group(7)),
                "type": m.group(9).strip(), "bonus_type": None,
                "maxblkid": None,
            }
            out[courant["id"]] = courant
            continue
        if courant is None:
            continue
        m = re.match(r"^\s+(\d+)\s+bonus\s+(.+?)\s*$", ligne)
        if m:
            courant["bonus_type"] = m.group(2).strip()
            courant["bonuslen"] = int(m.group(1))
            continue
        m = re.match(r"^\s*dnode maxblkid: (\d+)\s*$", ligne)
        if m:
            courant["maxblkid"] = int(m.group(1))
    return out


def datasets(pool: str, images_dir: str) -> dict[str, dict]:
    """Datasets listes par `zdb -d <pool>` : 'Dataset nom [ZPL], ID n, cr_txg t, taille, k objects'."""
    out = {}
    for ligne in run_zdb(["-d", pool], images_dir).splitlines():
        m = re.match(r"^Dataset (\S+) \[(\w+)\], ID (\d+), cr_txg (\d+), "
                     r"(\S+), (\d+) objects", ligne)
        if m:
            out[m.group(1)] = {"type": m.group(2), "id": int(m.group(3)),
                               "cr_txg": int(m.group(4)),
                               "objects": int(m.group(6))}
    return out
