"""
Topologie et representation explicite des vdev absents (MISSING),
pour differents scenarios de disques perdus.
"""

import glob
import itertools
import os
import tempfile
import unittest

from zfsrescue.topology import (STATE_AVAILABLE, STATE_MISSING, build_topology,
                                recoverability_summary, scan_devices)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))
LETTRES = [os.path.basename(p)[4] for p in IMAGES]     # a b c d


def topo_pour(images):
    scanned = scan_devices(images)
    try:
        return build_topology(scanned), scanned
    finally:
        pass


@unittest.skipUnless(len(IMAGES) == 4, "pool de test a 4 images absent")
class TestTopologie(unittest.TestCase):

    def _run(self, images):
        scanned = scan_devices(images)
        try:
            topo = build_topology(scanned)
            return topo, recoverability_summary(topo)
        finally:
            for s in scanned:
                s.device.close()

    def test_quatre_disques_presents(self):
        topo, rec = self._run(IMAGES)
        self.assertEqual(topo.name, "zrtest")
        self.assertEqual(len(topo.top_levels), 1)
        tl = topo.top_levels[0]
        self.assertEqual(tl.type, "raidz")
        self.assertEqual(tl.nparity, 1)
        self.assertEqual(tl.ncols, 4)
        self.assertEqual(tl.n_available, 4)
        self.assertEqual(tl.n_missing, 0)
        self.assertEqual(topo.inconsistencies, [])
        self.assertEqual(topo.warnings, [])
        self.assertTrue(rec["per_top_level_vdev"][0]["full_stripe_reconstructible"])

    def test_tous_les_couples_de_deux_disques(self):
        """Les 6 combinaisons de 2 disques survivants sur 4."""
        for couple in itertools.combinations(range(4), 2):
            images = [IMAGES[i] for i in couple]
            with self.subTest(survivants=[LETTRES[i] for i in couple]):
                topo, rec = self._run(images)
                tl = topo.top_levels[0]
                self.assertEqual(tl.ncols, 4)
                self.assertEqual(tl.n_available, 2)
                self.assertEqual(tl.n_missing, 2)
                dispo = {c.id for c in tl.children if c.state == STATE_AVAILABLE}
                self.assertEqual(dispo, set(couple),
                                 "les colonnes disponibles doivent correspondre "
                                 "aux images fournies")
                for c in tl.children:
                    if c.state == STATE_MISSING:
                        self.assertIsNone(c.device_path)
                        self.assertIsNotNone(c.guid)   # guid connu malgre l'absence
                self.assertFalse(
                    rec["per_top_level_vdev"][0]["full_stripe_reconstructible"])

    def test_un_seul_disque(self):
        topo, rec = self._run([IMAGES[2]])
        tl = topo.top_levels[0]
        self.assertEqual(tl.n_available, 1)
        self.assertEqual(tl.n_missing, 3)
        self.assertFalse(rec["per_top_level_vdev"][0]["full_stripe_reconstructible"])

    def test_parametres_de_mapping_raidz(self):
        topo, _ = self._run([IMAGES[2], IMAGES[3]])
        m = topo.top_levels[0].raidz_mapping_params()
        self.assertEqual(m["nparity"], 1)
        self.assertEqual(m["ncols"], 4)
        self.assertEqual(m["ashift"], 12)
        self.assertEqual(m["sector_size"], 4096)
        self.assertEqual(m["leaf_data_start"], 4 << 20)
        self.assertEqual(m["leaf_reserved_end"], 512 << 10)
        self.assertEqual(m["uberblock_size"], 4096)
        self.assertEqual(m["uberblocks_per_label"], 32)
        # asize du vdev = somme des asize des colonnes
        self.assertEqual(m["asize_total"], m["asize_per_child"] * m["ncols"])

    def test_asize_coherent_avec_la_taille_des_images(self):
        topo, _ = self._run(IMAGES)
        tl = topo.top_levels[0]
        for c in tl.children:
            self.assertEqual(c.asize, c.psize - (4 << 20) - (512 << 10))
        self.assertEqual(sum(c.asize for c in tl.children), tl.asize)

    def test_fichier_sans_label_zfs(self):
        with tempfile.TemporaryDirectory() as d:
            faux = os.path.join(d, "vide.img")
            with open(faux, "wb") as fh:
                fh.write(b"\x00" * (2 << 20))
            scanned = scan_devices([faux])
            try:
                topo = build_topology(scanned)
            finally:
                for s in scanned:
                    s.device.close()
            self.assertTrue(any("aucun label ZFS valide" in i
                                for i in topo.inconsistencies))
            self.assertIsNone(topo.name)

    def test_melange_image_valide_et_fichier_quelconque(self):
        with tempfile.TemporaryDirectory() as d:
            faux = os.path.join(d, "bruit.img")
            with open(faux, "wb") as fh:
                fh.write(os.urandom(2 << 20))
            scanned = scan_devices([IMAGES[2], faux])
            try:
                topo = build_topology(scanned)
            finally:
                for s in scanned:
                    s.device.close()
            self.assertEqual(topo.name, "zrtest")          # le pool reste identifie
            self.assertTrue(any("aucun label ZFS valide" in i
                                for i in topo.inconsistencies))
            tl = topo.top_levels[0]
            self.assertEqual(tl.n_available, 1)
            self.assertEqual(tl.n_missing, 3)
