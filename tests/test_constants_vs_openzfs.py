"""
Verifie que chaque constante de zfsrescue/consts.py correspond EXACTEMENT
au #define du source OpenZFS embarque dans vendor/.

Ce test est la garantie qu'aucune valeur n'a ete "devinee".
"""

import os
import re
import unittest

from zfsrescue import consts
from zfsrescue.label import label_offset, uberblock_shift

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_source() -> str | None:
    vendor = os.path.join(ROOT, "vendor")
    if not os.path.isdir(vendor):
        return None
    for name in sorted(os.listdir(vendor)):
        p = os.path.join(vendor, name)
        if os.path.isdir(p) and os.path.exists(
                os.path.join(p, "include/sys/vdev_impl.h")):
            return p
    return None


SRC = _find_source()


def cval2(case, expr):
    return cval(expr, {**case.vdev, **case.zio, **case.ub})


def defines(path: str) -> dict[str, str]:
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^#define\s+(\w+)\s+(.+?)\s*$", line)
            if m:
                val = re.sub(r"/\*.*?\*/", "", m.group(2))
                val = re.sub(r"//.*$", "", val).strip()
                out[m.group(1)] = val
    return out


def cval(expr: str, defs: dict[str, str] | None = None, depth: int = 0) -> int:
    """
    Evalue une expression C simple issue d'un #define :
    nombres, decalages, parentheses, suffixes ULL, autres macros, et
    sizeof(vdev_label_t) (recalcule a partir des membres declares dans le
    source, jamais depuis nos propres constantes).
    """
    if depth > 8:
        raise ValueError(f"substitution trop profonde : {expr!r}")
    defs = defs or {}
    e = re.sub(r"\b(\d+)(ULL|UL|LL|U|L)\b", r"\1", expr)

    if "sizeof (vdev_label_t)" in e or "sizeof(vdev_label_t)" in e:
        label_size = (2 * cval(defs["VDEV_PAD_SIZE"], defs, depth + 1)
                      + cval(defs["VDEV_PHYS_SIZE"], defs, depth + 1)
                      + cval(defs["VDEV_UBERBLOCK_RING"], defs, depth + 1))
        e = e.replace("sizeof (vdev_label_t)", str(label_size))
        e = e.replace("sizeof(vdev_label_t)", str(label_size))

    def sub_macro(m):
        name = m.group(0)
        if name not in defs:
            raise ValueError(f"macro inconnue : {name}")
        return "(" + str(cval(defs[name], defs, depth + 1)) + ")"

    e = re.sub(r"\b[A-Z_][A-Z0-9_]*\b", sub_macro, e)
    if not re.fullmatch(r"[\d\s()<>+*/-]+", e):
        raise ValueError(f"expression non evaluable : {expr!r}")
    return int(eval(e, {"__builtins__": {}}))


@unittest.skipIf(SRC is None, "source OpenZFS absent de vendor/")
class TestConstantsMatchOpenZFS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vdev = defines(os.path.join(SRC, "include/sys/vdev_impl.h"))
        cls.zio = defines(os.path.join(SRC, "include/sys/zio.h"))
        cls.ub = defines(os.path.join(SRC, "include/sys/uberblock_impl.h"))

    def test_label_geometry(self):
        self.assertEqual(cval2(self, self.vdev["VDEV_PAD_SIZE"]), consts.VDEV_PAD_SIZE)
        self.assertEqual(cval2(self, self.vdev["VDEV_PHYS_SIZE"]), consts.VDEV_PHYS_SIZE)
        self.assertEqual(cval2(self, self.vdev["VDEV_UBERBLOCK_RING"]),
                         consts.VDEV_UBERBLOCK_RING)
        self.assertEqual(cval2(self, self.vdev["VDEV_BOOT_SIZE"]), consts.VDEV_BOOT_SIZE)
        self.assertEqual(cval2(self, self.vdev["VDEV_LABELS"]), consts.VDEV_LABELS)
        self.assertEqual(cval2(self, self.vdev["VDEV_LABEL_START_SIZE"]),
                         consts.VDEV_LABEL_START_SIZE)
        self.assertEqual(cval2(self, self.vdev["VDEV_LABEL_END_SIZE"]),
                         consts.VDEV_LABEL_END_SIZE)

    def test_vdev_label_t_size(self):
        """vdev_label_t = pad1 + vl_be + vdev_phys + anneau d'uberblocks."""
        total = (cval2(self, self.vdev["VDEV_PAD_SIZE"]) * 2
                 + cval2(self, self.vdev["VDEV_PHYS_SIZE"])
                 + cval2(self, self.vdev["VDEV_UBERBLOCK_RING"]))
        self.assertEqual(total, consts.VDEV_LABEL_SIZE)
        self.assertEqual(consts.VDEV_LABEL_SIZE, 256 * 1024)

    def test_magics(self):
        self.assertEqual(int(self.zio["ZEC_MAGIC"].replace("ULL", ""), 16),
                         consts.ZEC_MAGIC)
        self.assertEqual(int(self.ub["UBERBLOCK_MAGIC"].replace("ULL", ""), 16),
                         consts.UBERBLOCK_MAGIC)

    def test_uberblock_shift_rule(self):
        """include/sys/vdev_impl.h : MIN(MAX(ashift, UBERBLOCK_SHIFT), MAX_UBERBLOCK_SHIFT)"""
        self.assertEqual(cval2(self, self.vdev["MAX_UBERBLOCK_SHIFT"]),
                         consts.MAX_UBERBLOCK_SHIFT)
        self.assertEqual(cval2(self, self.ub["UBERBLOCK_SHIFT"]), consts.UBERBLOCK_SHIFT)
        self.assertEqual(uberblock_shift(9), 10)    # ashift 9  -> 1 Kio
        self.assertEqual(uberblock_shift(12), 12)   # ashift 12 -> 4 Kio
        self.assertEqual(uberblock_shift(13), 13)   # ashift 13 -> 8 Kio
        self.assertEqual(uberblock_shift(16), 13)   # plafonne a 8 Kio


class TestLabelOffsetFormula(unittest.TestCase):
    """module/zfs/vdev_label.c:163 — vdev_label_offset()."""

    def test_offsets_512M(self):
        psize = 512 << 20
        self.assertEqual(label_offset(psize, 0), 0)
        self.assertEqual(label_offset(psize, 1), 256 << 10)
        self.assertEqual(label_offset(psize, 2), psize - (512 << 10))
        self.assertEqual(label_offset(psize, 3), psize - (256 << 10))

    def test_offsets_with_inner_offset(self):
        psize = 4 << 30
        self.assertEqual(label_offset(psize, 0, consts.OFFSETOF_VDEV_PHYS),
                         16 << 10)
        self.assertEqual(label_offset(psize, 3, consts.OFFSETOF_UBERBLOCK_RING),
                         psize - (256 << 10) + (128 << 10))

    def test_rejects_unaligned_psize(self):
        with self.assertRaises(ValueError):
            label_offset((512 << 20) + 1, 0)

    def test_rejects_bad_index(self):
        with self.assertRaises(ValueError):
            label_offset(512 << 20, 4)
