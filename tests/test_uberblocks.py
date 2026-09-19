"""
Etape 4 — uberblocks.

Oracle : `zdb -lu`. Verifie aussi les regles de format (creneau = txg % n,
checksum auto-porte, somme des guid) et la detection d'une expansion RAIDZ.
"""

import glob
import os
import struct
import unittest

from zfsrescue import consts
from zfsrescue.blkptr import parse_blkptr
from zfsrescue.label import read_all_labels, uberblock_shift
from zfsrescue.readonly import ReadOnlyDevice
from zfsrescue.uberblock import (MMP_MAGIC, UberblockSet, guid_sum_expected,
                                 parse_uberblock, read_uberblocks)

from . import oracle_zdb as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))
ASHIFT = 12


def lire(images):
    s = UberblockSet()
    for p in images:
        with ReadOnlyDevice(p) as dev:
            s.all += read_uberblocks(dev, ASHIFT)
    return s


@unittest.skipUnless(IMAGES, "pool de test absent")
class TestLectureDesUberblocks(unittest.TestCase):

    def test_geometrie_de_l_anneau(self):
        """32 creneaux de 4096 octets par label pour ashift=12."""
        self.assertEqual(uberblock_shift(ASHIFT), 12)
        nombre = consts.VDEV_UBERBLOCK_RING >> uberblock_shift(ASHIFT)
        self.assertEqual(nombre, 32)
        with ReadOnlyDevice(IMAGES[0]) as dev:
            ubs = read_uberblocks(dev, ASHIFT)
        self.assertEqual(len(ubs), 4 * nombre)
        self.assertTrue(all(u.slot_size == 4096 for u in ubs))

    def test_creneau_egal_txg_modulo_nombre(self):
        """vdev_label.c:1793 — n = txg % VDEV_UBERBLOCK_COUNT."""
        s = lire(IMAGES)
        for u in s.valid:
            self.assertEqual(u.slot, u.txg % 32,
                             f"txg {u.txg} devrait occuper le creneau "
                             f"{u.txg % 32}, trouve {u.slot}")

    def test_tous_les_uberblocks_presents_sont_valides(self):
        s = lire(IMAGES)
        self.assertEqual(s.corrupt, [], "aucun uberblock corrompu attendu")
        self.assertGreater(len(s.valid), 0)

    def test_les_quatre_labels_portent_les_memes_uberblocks(self):
        with ReadOnlyDevice(IMAGES[0]) as dev:
            ubs = read_uberblocks(dev, ASHIFT)
        par_label = {}
        for u in ubs:
            if u.valid:
                par_label.setdefault(u.label_index, {})[u.txg] = u.identity
        self.assertEqual(len(par_label), 4)
        reference = par_label[0]
        for l in (1, 2, 3):
            self.assertEqual(par_label[l], reference,
                             f"le label {l} differe du label 0")

    def test_somme_des_guid_coherente(self):
        """
        ub_guid_sum = somme des guid de TOUS les vdev de l'arbre (vdev.c:573).
        Une divergence signalerait un vdev inconnu de nous.
        """
        with ReadOnlyDevice(IMAGES[0]) as dev:
            cfg = read_all_labels(dev)[0].config
            ubs = read_uberblocks(dev, ASHIFT)
        tree = cfg["vdev_tree"]
        attendu = guid_sum_expected(cfg["pool_guid"], [tree["guid"]],
                                    [c["guid"] for c in tree["children"]])
        for u in ubs:
            if u.valid:
                self.assertEqual(u.guid_sum, attendu)

    def test_pas_d_expansion_raidz_ni_de_checkpoint(self):
        s = lire(IMAGES)
        for u in s.valid:
            self.assertEqual(u.raidz_reflow_info, 0)
            self.assertFalse(u.raidz_expansion_active)
            self.assertEqual(u.checkpoint_txg, 0)

    def test_mmp_desactive(self):
        s = lire(IMAGES)
        for u in s.valid:
            self.assertEqual(u.mmp_magic, MMP_MAGIC)
            self.assertFalse(u.mmp_enabled)

    def test_meilleur_uberblock_coherent_avec_le_label(self):
        with ReadOnlyDevice(IMAGES[0]) as dev:
            txg_label = read_all_labels(dev)[0].config["txg"]
        s = lire(IMAGES)
        self.assertEqual(s.best.txg, txg_label)
        self.assertFalse(s.best.rootbp.is_hole)
        self.assertEqual(s.best.rootbp.type_name, "objset")
        self.assertEqual(s.best.rootbp.level, 0)

    def test_deux_disques_suffisent_pour_les_uberblocks(self):
        """Les uberblocks sont dans les labels : ils survivent a la perte de 2 disques."""
        avec_4 = lire(IMAGES)
        avec_2 = lire(IMAGES[2:])
        self.assertEqual({u.txg for u in avec_2.valid},
                         {u.txg for u in avec_4.valid})
        self.assertEqual(avec_2.best.identity, avec_4.best.identity)


@unittest.skipUnless(IMAGES and Z.available, "images ou zdb absents")
class TestComparaisonAvecZdb(unittest.TestCase):

    def test_liste_identique_a_zdb(self):
        for img in IMAGES:
            with self.subTest(image=os.path.basename(img)):
                ref = {u["slot"]: u for u in Z.uberblocks(img)}
                with ReadOnlyDevice(img) as dev:
                    nos = [u for u in read_uberblocks(dev, ASHIFT) if u.valid]
                par_creneau = {}
                for u in nos:
                    par_creneau.setdefault(u.slot, []).append(u)
                self.assertEqual(set(par_creneau), set(ref),
                                 "memes creneaux occupes que zdb")
                for slot, ubs in par_creneau.items():
                    r = ref[slot]
                    for u in ubs:
                        self.assertEqual(u.txg, r["txg"])
                        self.assertEqual(u.version, r["version"])
                        self.assertEqual(u.guid_sum, r["guid_sum"])
                        self.assertEqual(u.timestamp, r["timestamp"])
                        self.assertEqual(u.checkpoint_txg, r["checkpoint_txg"])
                        self.assertEqual(u.raidz_reflow_state,
                                         r.get("reflow_state", 0))
                    self.assertEqual(sorted({u.label_index for u in ubs}),
                                     r["labels"])

    def test_rootbp_identique_a_zdb(self):
        img = IMAGES[0]
        ref = {u["slot"]: u for u in Z.uberblocks(img)}
        with ReadOnlyDevice(img) as dev:
            for u in read_uberblocks(dev, ASHIFT):
                if not u.valid or u.label_index != 0:
                    continue
                r = ref[u.slot]
                with self.subTest(slot=u.slot):
                    nos_dvas = [(d.vdev, d.offset, d.asize)
                                for d in u.rootbp.dvas if not d.is_empty]
                    self.assertEqual(nos_dvas, r["bp_dvas"])
                    self.assertEqual(list(u.rootbp.cksum), r["bp_cksum"])
                    self.assertEqual(u.rootbp.lsize, 0x1000)
                    self.assertEqual(u.rootbp.psize, 0x1000)
                    self.assertEqual(u.rootbp.compress_name, "off")
                    self.assertEqual(u.rootbp.checksum_name, "fletcher_4")
                    self.assertEqual(u.rootbp.byteorder, 1)   # LE


class TestRobustesse(unittest.TestCase):

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_corruption_detectee(self):
        """Un octet modifie doit invalider l'uberblock (en memoire uniquement)."""
        with ReadOnlyDevice(IMAGES[0]) as dev:
            base = 128 * 1024
            brut = dev.pread(base + 9 * 4096, 4096)      # creneau 9 = txg 41
        bon = parse_uberblock(brut, "test", 0, 9, base + 9 * 4096)
        self.assertTrue(bon.valid)
        for pos in (16, 100, 2000, 4000):
            casse = bytearray(brut)
            casse[pos] ^= 0xFF
            u = parse_uberblock(bytes(casse), "test", 0, 9, base + 9 * 4096)
            self.assertFalse(u.valid, f"corruption a l'octet {pos} non detectee")

    def test_creneau_vide(self):
        u = parse_uberblock(b"\x00" * 4096, "test", 0, 0, 0)
        self.assertFalse(u.magic_ok)
        self.assertFalse(u.valid)
        self.assertEqual(u.txg, 0)

    def test_uberblock_big_endian_detecte(self):
        """Un uberblock ecrit par une machine big-endian est reconnu comme tel."""
        brut = bytearray(4096)
        struct.pack_into(">Q", brut, 0, consts.UBERBLOCK_MAGIC)
        u = parse_uberblock(bytes(brut), "test", 0, 0, 0)
        self.assertTrue(u.magic_ok)
        self.assertTrue(u.byteswapped)
        self.assertFalse(u.valid)      # checksum absent : jamais considere valide
