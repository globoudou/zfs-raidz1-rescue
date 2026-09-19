#!/usr/bin/env python3
"""
Genere zfsrescue/zfs_enums.py a partir des en-tetes OpenZFS embarques dans
vendor/. Aucune valeur n'est recopiee a la main : elles sont extraites du
source, ce qui garantit qu'elles suivent la version de reference.

    python3 scripts/40_gen_enums.py            # regenere le module
    python3 scripts/40_gen_enums.py --check    # verifie qu'il est a jour
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CIBLE = os.path.join(ROOT, "zfsrescue", "zfs_enums.py")


def source_dir() -> str:
    vendor = os.path.join(ROOT, "vendor")
    for name in sorted(os.listdir(vendor)):
        p = os.path.join(vendor, name)
        if os.path.exists(os.path.join(p, "include/sys/spa.h")):
            return p
    raise SystemExit("source OpenZFS introuvable dans vendor/")


def enum_simple(texte: str, nom: str, prefixe: str) -> dict[int, str]:
    """Extrait un enum C dont les membres sont implicitement numerotes."""
    m = re.search(r"enum\s+" + nom + r"\s*\{(.*?)\n\}", texte, re.S)
    if not m:
        raise SystemExit(f"enum {nom} introuvable")
    val, out = 0, {}
    for ligne in m.group(1).splitlines():
        ligne = re.sub(r"/\*.*?\*/", "", ligne).strip().rstrip(",")
        if not ligne or ligne.startswith(("/*", "*", "#")):
            continue
        mm = re.match(r"^(" + prefixe + r"\w*)\s*(?:=\s*([^,]+))?$", ligne)
        if not mm:
            continue
        nom_membre, expr = mm.group(1), mm.group(2)
        if expr is not None:
            expr = expr.strip()
            if re.fullmatch(r"\d+", expr):
                val = int(expr)
            elif re.fullmatch(r"0x[0-9a-fA-F]+", expr):
                val = int(expr, 16)
            else:
                continue          # alias sur un autre membre : ignore
        out[val] = nom_membre
        val += 1
    return out


def defines(texte: str, noms: list[str]) -> dict[str, int]:
    out = {}
    for n in noms:
        m = re.search(r"^#define\s+" + n + r"\s+(.+?)\s*(?:/\*.*)?$",
                      texte, re.M)
        if not m:
            raise SystemExit(f"#define {n} introuvable")
        v = m.group(1).strip().rstrip("ULL").rstrip("U").strip()
        v = re.sub(r"/\*.*?\*/", "", v).strip()
        if re.fullmatch(r"0x[0-9a-fA-F]+", v):
            out[n] = int(v, 16)
        elif re.fullmatch(r"\d+", v):
            out[n] = int(v)
        elif re.fullmatch(r"\(1ULL << (\w+)\)", v):
            out[n] = 1 << out[re.fullmatch(r"\(1ULL << (\w+)\)", v).group(1)]
        else:
            raise SystemExit(f"#define {n} = {v!r} non evaluable")
    return out


def generer() -> str:
    S = source_dir()
    spa = open(os.path.join(S, "include/sys/spa.h")).read()
    zio = open(os.path.join(S, "include/sys/zio.h")).read()
    zioc = open(os.path.join(S, "include/sys/zio_compress.h")).read()
    dmu = open(os.path.join(S, "include/sys/dmu.h")).read()
    zfs = open(os.path.join(S, "include/sys/fs/zfs.h")).read()

    compress = enum_simple(zioc, "zio_compress", "ZIO_COMPRESS_")
    checksum = enum_simple(zio, "zio_checksum", "ZIO_CHECKSUM_")
    dmu_types = enum_simple(dmu, "dmu_object_type", "DMU_OT_")
    cst = {}
    cst.update(defines(spa, ["SPA_LSIZEBITS", "SPA_PSIZEBITS", "SPA_ASIZEBITS",
                             "SPA_COMPRESSBITS", "SPA_VDEVBITS",
                             "SPA_DVAS_PER_BP", "SPA_BLKPTRSHIFT"]))
    cst.update(defines(zfs, ["SPA_MINBLOCKSHIFT"]))
    cst.update(defines(dmu, ["DMU_OT_NEWTYPE", "DMU_OT_METADATA",
                             "DMU_OT_ENCRYPTED", "DMU_OT_BYTESWAP_MASK"]))

    version = os.path.basename(S)
    L = [
        '"""',
        "Enumerations et constantes ZFS EXTRAITES AUTOMATIQUEMENT du source.",
        "",
        f"Source : vendor/{version} (en-tetes spa.h, zio.h, zio_compress.h,",
        "dmu.h, fs/zfs.h).",
        "",
        "NE PAS EDITER A LA MAIN : regenerer avec",
        "    python3 scripts/40_gen_enums.py",
        '"""',
        "",
        f'OPENZFS_SOURCE = "{version}"',
        "",
    ]
    for nom, prefixe, valeurs in (("ZIO_COMPRESS", "ZIO_COMPRESS_", compress),
                                  ("ZIO_CHECKSUM", "ZIO_CHECKSUM_", checksum),
                                  ("DMU_OBJECT_TYPE", "DMU_OT_", dmu_types)):
        L.append(f"{nom} = {{")
        for k in sorted(valeurs):
            L.append(f'    {k}: "{valeurs[k]}",')
        L.append("}")
        L.append("")
        court = {k: v.replace(prefixe, "", 1).lower() for k, v in valeurs.items()}
        L.append(f"{nom}_NAMES = {{")
        for k in sorted(court):
            L.append(f'    {k}: "{court[k]}",')
        L.append("}")
        L.append("")
    for k in sorted(cst):
        L.append(f"{k} = {cst[k]}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    contenu = generer()
    if args.check:
        actuel = open(CIBLE).read() if os.path.exists(CIBLE) else ""
        if actuel != contenu:
            print("zfsrescue/zfs_enums.py n'est pas a jour "
                  "(relancer scripts/40_gen_enums.py)", file=sys.stderr)
            return 1
        print("zfs_enums.py est a jour")
        return 0
    with open(CIBLE, "w", encoding="utf-8") as fh:
        fh.write(contenu)
    print(f"ecrit : {CIBLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
