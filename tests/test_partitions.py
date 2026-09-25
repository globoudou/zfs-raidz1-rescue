"""
Vdev place dans une PARTITION (cas FreeBSD / FreeNAS / TrueNAS).

Les labels se trouvent alors au debut et a la fin de la partition, pas du
disque. L'outil doit le detecter seul, et le dire.
"""

import glob
import os
import shutil
import tempfile
import unittest

from zfsrescue.parttable import read_partition_table
from zfsrescue.pool import open_pool
from zfsrescue.readonly import ReadOnlyDevice, ReadOnlyError
from zfsrescue.topology import build_topology, scan_devices

from .gpt_fixture import (FREEBSD_SWAP, FREEBSD_ZFS, copier_labels,
                          creer_image_gpt)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))


class TestFenetreDeLecture(unittest.TestCase):
    """Lire une partie du support ne doit rien changer aux couches du dessus."""

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_fenetre_equivalente_a_une_image_complete(self):
        with ReadOnlyDevice(IMAGES[0]) as entier, \
             ReadOnlyDevice(IMAGES[0], offset=1 << 20, length=8 << 20) as f:
            self.assertFalse(entier.info.windowed)
            self.assertTrue(f.info.windowed)
            self.assertEqual(f.info.size, 8 << 20)
            self.assertEqual(f.info.media_size, entier.info.size)
            self.assertEqual(f.pread(0, 4096), entier.pread(1 << 20, 4096))
            with self.assertRaises(ReadOnlyError):
                f.pread((8 << 20) - 10, 100)      # borne de la fenetre

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_offset_invalide(self):
        taille = os.path.getsize(IMAGES[0])
        with self.assertRaises(ReadOnlyError):
            ReadOnlyDevice(IMAGES[0], offset=taille)
        with self.assertRaises(ReadOnlyError):
            ReadOnlyDevice(IMAGES[0], offset=-1)


class TestTableDePartitions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gpt(self):
        img = os.path.join(self.tmp, "gpt.img")
        offs = creer_image_gpt(img, [(FREEBSD_SWAP, 2 << 20, "swap0"),
                                     (FREEBSD_ZFS, 16 << 20, "zfs0")])
        with ReadOnlyDevice(img) as dev:
            t = read_partition_table(dev.pread, dev.info.size)
        self.assertEqual(t.kind, "gpt")
        self.assertEqual(t.sector_size, 512)
        self.assertEqual(t.errors, [], "les CRC doivent etre corrects")
        self.assertEqual(len(t.partitions), 2)
        p1, p2 = t.partitions
        self.assertEqual((p1.index, p1.start, p1.type_name, p1.name),
                         (1, offs[1], "FreeBSD swap", "swap0"))
        self.assertEqual((p2.index, p2.start, p2.type_name, p2.name),
                         (2, offs[2], "FreeBSD ZFS", "zfs0"))
        self.assertFalse(p1.likely_zfs)
        self.assertTrue(p2.likely_zfs)
        self.assertEqual([p.index for p in t.zfs_candidates][0], 2,
                         "la partition ZFS doit etre essayee en premier")

    def test_crc_corrompu_signale(self):
        img = os.path.join(self.tmp, "gpt.img")
        creer_image_gpt(img, [(FREEBSD_ZFS, 8 << 20, "zfs0")])
        with open(img, "r+b") as fh:            # abime une entree de la table
            fh.seek(2 * 512 + 60)
            fh.write(b"\xff\xff")
        with ReadOnlyDevice(img) as dev:
            t = read_partition_table(dev.pread, dev.info.size)
        self.assertTrue(any("CRC" in e for e in t.errors), t.errors)

    def test_support_sans_table(self):
        img = os.path.join(self.tmp, "brut.img")
        with open(img, "wb") as fh:
            fh.write(b"\x00" * (4 << 20))
        with ReadOnlyDevice(img) as dev:
            t = read_partition_table(dev.pread, dev.info.size)
        self.assertIsNone(t.kind)
        self.assertEqual(t.partitions, [])


@unittest.skipUnless(IMAGES, "pool de test absent")
class TestDetectionAutomatique(unittest.TestCase):
    """
    Reproduit la disposition rencontree sur les vrais disques : GPT avec une
    partition « FreeBSD swap » puis une partition « FreeBSD ZFS ».
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conteneurs = []
        for src in IMAGES[2:]:                  # les deux survivants suffisent
            taille = os.path.getsize(src)
            img = os.path.join(cls.tmp, os.path.basename(src))
            offs = creer_image_gpt(img, [(FREEBSD_SWAP, 2 << 20, "swap0"),
                                         (FREEBSD_ZFS, taille, "zfs0")])
            # creux, sauf les zones de label : inutile de copier 512 Mio
            copier_labels(img, offs[2], src, taille)
            cls.conteneurs.append((img, offs[2]))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_labels_trouves_dans_la_partition(self):
        chemins = [c for c, _o in self.conteneurs]
        scanned = scan_devices(chemins)
        try:
            for sd, (_c, offset) in zip(scanned, self.conteneurs):
                self.assertEqual(len(sd.valid_labels), 4)
                self.assertIsNotNone(sd.partition)
                self.assertEqual(sd.partition.index, 2)
                self.assertEqual(sd.partition.start, offset)
                self.assertEqual(sd.partition.type_name, "FreeBSD ZFS")
                self.assertTrue(any("partition 2" in n for n in sd.notes))
                self.assertTrue(sd.device.info.windowed)
            topo = build_topology(scanned)
            self.assertEqual(topo.name, "zrtest")
            self.assertEqual(topo.top_levels[0].ncols, 4)
            self.assertEqual(topo.top_levels[0].n_available, 2)
        finally:
            for sd in scanned:
                sd.device.close()

    def test_option_offset_explicite(self):
        chemin, offset = self.conteneurs[0]
        scanned = scan_devices([chemin], offsets={chemin: offset})
        try:
            self.assertEqual(len(scanned[0].valid_labels), 4)
            self.assertTrue(any("offset" in n for n in scanned[0].notes))
        finally:
            scanned[0].device.close()

    def test_scan_desactivable(self):
        chemin, _offset = self.conteneurs[0]
        scanned = scan_devices([chemin], scan_partitions=False)
        try:
            self.assertEqual(len(scanned[0].valid_labels), 0)
            self.assertIsNone(scanned[0].partition)
        finally:
            scanned[0].device.close()

    def test_pool_complet_utilisable_depuis_les_partitions(self):
        chemins = [c for c, _o in self.conteneurs]
        # le MOS n'est pas lisible ici (seules les zones de label sont copiees),
        # mais l'ouverture doit aller jusqu'aux uberblocks sans se tromper
        from zfsrescue.uberblock import UberblockSet, read_uberblocks
        scanned = scan_devices(chemins)
        try:
            jeu = UberblockSet()
            for sd in scanned:
                jeu.all += read_uberblocks(sd.device, 12)
            self.assertGreater(len(jeu.valid), 0)
            self.assertEqual(jeu.best.txg, 41)
        finally:
            for sd in scanned:
                sd.device.close()


class TestDiagnosticSansLabel(unittest.TestCase):
    """Le message doit dire qu'il n'y a pas de label, pas evoquer un nvlist casse."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_message_explicite(self):
        img = os.path.join(self.tmp, "sda.img")
        creer_image_gpt(img, [(FREEBSD_SWAP, 2 << 20, "swap0"),
                              (FREEBSD_ZFS, 32 << 20, "zfs0")])
        scanned = scan_devices([img])
        try:
            sd = scanned[0]
            self.assertEqual(len(sd.valid_labels), 0)
            for lab in sd.labels:
                texte = " ".join(lab.errors)
                self.assertIn("emplacement de label", texte)
                self.assertNotIn("encodage nvlist", texte)
            self.assertTrue(any("GPT" in n for n in sd.notes))
            self.assertIsNotNone(sd.partition_table)
            self.assertTrue(any(p.likely_zfs
                                for p in sd.partition_table.partitions))
        finally:
            scanned[0].device.close()
