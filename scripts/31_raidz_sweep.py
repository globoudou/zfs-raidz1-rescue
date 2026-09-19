#!/usr/bin/env python3
"""
Campagne differentielle exhaustive du mapping RAIDZ contre `zdb -R`.

Plus lente que la suite de tests (un processus zdb par combinaison), elle est
donc separee. Elle ne fait que LIRE les images.

    python3 scripts/31_raidz_sweep.py [--sizes ...] [--offsets ...]
"""
import argparse
import itertools
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfsrescue.raidz import assemble_data, raidz_map_alloc, read_columns
from zfsrescue.readonly import ReadOnlyDevice

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "testlab/images")
ZDB = shutil.which("zdb") or "/usr/sbin/zdb"

TAILLES = [0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x7000, 0x8000,
           0xC000, 0x10000, 0x1F000, 0x20000, 0x21000, 0x40000, 0x80000, 0x100000]
OFFSETS = [0x0, 0x1000, 0x2000, 0x3000,
           0x100000, 0x101000, 0x102000, 0x103000,
           0x400000, 0x401000, 0x1A16C000, 0x1A16D000,
           0x40000000, 0x40001000, 0x7D000000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="zrtest")
    ap.add_argument("--ashift", type=int, default=12)
    ap.add_argument("--ncols", type=int, default=4)
    ap.add_argument("--nparity", type=int, default=1)
    args = ap.parse_args()

    devs = {i: ReadOnlyDevice(os.path.join(IMAGES_DIR, f"disk{c}.img"))
            for i, c in enumerate("abcd")}
    ok = ko = 0
    t0 = time.time()
    try:
        for off, size in itertools.product(OFFSETS, TAILLES):
            rm = raidz_map_alloc(off, size, args.ashift, args.ncols, args.nparity)
            nous = assemble_data(rm, read_columns(rm, devs))
            ref = subprocess.run(
                [ZDB, "-e", "-p", IMAGES_DIR, "-R", args.pool,
                 f"0:{off:x}:{size:x}:r"], capture_output=True).stdout
            if nous == ref and len(ref) == size:
                ok += 1
            else:
                ko += 1
                print(f"ECART offset={off:#x} taille={size:#x} "
                      f"(nous {len(nous or b'')} o, zdb {len(ref)} o)")
    finally:
        for d in devs.values():
            d.close()

    print(f"{ok} combinaisons identiques a zdb, {ko} ecart(s), "
          f"en {time.time() - t0:.0f} s")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
