"""
Compare notre lecture des labels a celle de `zdb -l` (outil officiel OpenZFS),
sur les 4 images du pool de test.
"""

import glob
import os
import unittest

from zfsrescue import consts
from zfsrescue.label import read_all_labels, verify_embedded_checksum
from zfsrescue.readonly import ReadOnlyDevice

from . import oracle_zdb as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))


@unittest.skipUnless(IMAGES, "pool de test absent (scripts/10_make_test_pool.sh)")
class TestLabelsMatchZdb(unittest.TestCase):

    @unittest.skipUnless(Z.available, "zdb indisponible")
    def test_config_identique_a_zdb(self):
        for img in IMAGES:
            with self.subTest(image=os.path.basename(img)):
                ref = Z.label_configs(img)[0]
                with ReadOnlyDevice(img) as dev:
                    labels = read_all_labels(dev)
                for lab in labels:
                    cfg = lab.config
                    for key in ("version", "name", "state", "txg", "pool_guid",
                                "errata", "hostid", "hostname", "top_guid",
                                "guid", "vdev_children"):
                        self.assertEqual(cfg.get(key), ref.get(key),
                                         f"{img} L{lab.index} champ {key}")
                    ours, theirs = cfg["vdev_tree"], ref["vdev_tree"]
                    for key in ("type", "id", "guid", "nparity", "ashift",
                                "asize", "metaslab_array", "metaslab_shift",
                                "is_log", "create_txg"):
                        self.assertEqual(ours.get(key), theirs.get(key),
                                         f"{img} L{lab.index} vdev_tree.{key}")
                    self.assertEqual(len(ours["children"]),
                                     len(theirs["children"]))
                    for a, b in zip(ours["children"], theirs["children"]):
                        self.assertEqual(a["guid"], b["guid"])
                        self.assertEqual(a["id"], b["id"])
                        self.assertEqual(a["path"], b["path"])
                        self.assertEqual(a["type"], b["type"])

    def test_les_quatre_labels_sont_valides(self):
        for img in IMAGES:
            with self.subTest(image=os.path.basename(img)):
                with ReadOnlyDevice(img) as dev:
                    labels = read_all_labels(dev)
                self.assertEqual(len(labels), 4)
                for lab in labels:
                    self.assertTrue(lab.checksum.magic_ok,
                                    f"L{lab.index} : zec_magic absent")
                    self.assertTrue(lab.checksum.valid,
                                    f"L{lab.index} : checksum SHA-256 invalide")
                    self.assertFalse(lab.checksum.byteswapped)
                    self.assertTrue(lab.valid)
                    self.assertEqual(lab.nvlist.anomalies, [])

    def test_les_quatre_labels_ont_la_meme_config(self):
        for img in IMAGES:
            with ReadOnlyDevice(img) as dev:
                cfgs = [l.config for l in read_all_labels(dev)]
            for c in cfgs[1:]:
                self.assertEqual(c, cfgs[0],
                                 f"{img}: les 4 labels doivent etre identiques")

    def test_offsets_des_labels(self):
        with ReadOnlyDevice(IMAGES[0]) as dev:
            psize = dev.info.psize
            labels = read_all_labels(dev)
        attendus = [0, consts.VDEV_LABEL_SIZE,
                    psize - 2 * consts.VDEV_LABEL_SIZE,
                    psize - consts.VDEV_LABEL_SIZE]
        self.assertEqual([l.offset for l in labels], attendus)

    def test_checksum_detecte_une_corruption(self):
        """Un seul bit modifie doit invalider le checksum (en memoire, jamais sur disque)."""
        with ReadOnlyDevice(IMAGES[0]) as dev:
            raw = dev.pread(consts.OFFSETOF_VDEV_PHYS, consts.VDEV_PHYS_SIZE)
        self.assertTrue(verify_embedded_checksum(raw, consts.OFFSETOF_VDEV_PHYS).valid)
        for pos in (0, 100, len(raw) // 2, len(raw) - consts.ZIO_ECK_SIZE - 1):
            corrompu = bytearray(raw)
            corrompu[pos] ^= 0x01
            res = verify_embedded_checksum(bytes(corrompu), consts.OFFSETOF_VDEV_PHYS)
            self.assertFalse(res.valid, f"corruption a l'octet {pos} non detectee")

    def test_checksum_depend_de_l_offset(self):
        """Le verifieur est l'offset physique : une mauvaise position invalide tout."""
        with ReadOnlyDevice(IMAGES[0]) as dev:
            raw = dev.pread(consts.OFFSETOF_VDEV_PHYS, consts.VDEV_PHYS_SIZE)
        mauvais = consts.VDEV_LABEL_SIZE + consts.OFFSETOF_VDEV_PHYS
        self.assertFalse(verify_embedded_checksum(raw, mauvais).valid)
