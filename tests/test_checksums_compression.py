"""
Checksums et decompression, valides contre des blocs reels du pool.

Oracle : `zdb -R ... :d` decompresse un bloc avec le code d'OpenZFS.
"""

import glob
import os
import subprocess
import unittest

from zfsrescue import checksum as cks
from zfsrescue import compress as cmp
from zfsrescue.pool import open_pool

from . import oracle_zdb as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "testlab/images")
IMAGES = sorted(glob.glob(os.path.join(IMAGES_DIR, "disk?.img")))


class TestChecksums(unittest.TestCase):

    def test_fletcher4_vecteur_connu(self):
        """Somme de controle d'un tampon nul : tout doit rester a zero."""
        self.assertEqual(cks.fletcher_4(b"\x00" * 4096), (0, 0, 0, 0))
        self.assertEqual(cks.fletcher_2(b"\x00" * 4096), (0, 0, 0, 0))

    def test_fletcher4_incremental(self):
        """a, b, c, d sont des sommes cumulees successives (zfs_fletcher.c:318)."""
        data = b"\x01\x00\x00\x00" * 3          # trois mots valant 1
        self.assertEqual(cks.fletcher_4(data), (3, 6, 10, 15))

    def test_fletcher2_par_paires(self):
        data = (b"\x01" + b"\x00" * 7) * 4      # quatre mots de 64 bits valant 1
        self.assertEqual(cks.fletcher_2(data), (2, 2, 3, 3))

    def test_sha256_mots_en_big_endian(self):
        import hashlib
        import struct
        d = b"zfs"
        self.assertEqual(cks.sha256(d),
                         struct.unpack(">4Q", hashlib.sha256(d).digest()))

    def test_algorithme_non_supporte_est_signale(self):
        with self.assertRaises(cks.ChecksumNonSupporte):
            cks.compute(12, b"\x00" * 512)      # skein

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_checksums_de_tous_les_blocs_du_mos(self):
        p = open_pool(IMAGES)
        try:
            verifies = 0
            for oid, dn, _e in p.dmu.iter_dnodes(p.mos.meta_dnode):
                if dn is None or not dn.allocated:
                    continue
                for bp in dn.blkptrs:
                    if bp.is_hole or bp.embedded:
                        continue
                    r = p.reader.read_block(bp)
                    self.assertTrue(r.checksum_ok, f"objet {oid}")
                    verifies += 1
            self.assertGreater(verifies, 20)
        finally:
            p.close()


class TestDecompression(unittest.TestCase):

    def test_zle(self):
        # 3 octets litteraux puis 5 zeros (niveau 64)
        src = bytes([2, 0x41, 0x42, 0x43, 64 + 5 - 1])
        self.assertEqual(cmp.zle_decompress(src, 8), b"ABC" + b"\x00" * 5)

    def test_lz4_litteraux_seuls(self):
        charge = b"ABCDEFGH"
        bloc = bytes([len(charge) << 4]) + charge
        src = len(bloc).to_bytes(4, "big") + bloc
        self.assertEqual(cmp.lz4_decompress(src, len(charge)), charge)

    def test_lz4_avec_repetition(self):
        # 4 litteraux "ABCD" puis un match de 4 octets a l'offset 4
        bloc = bytes([(4 << 4) | 0]) + b"ABCD" + (4).to_bytes(2, "little") \
            + bytes([0])
        src = len(bloc).to_bytes(4, "big") + bloc
        self.assertEqual(cmp.lz4_decompress(src, 8), b"ABCDABCD")

    def test_non_compresse(self):
        self.assertEqual(cmp.decompress(2, b"abcdef", 4), b"abcd")

    def test_zstd_signale_l_absence_de_decodeur(self):
        if cmp.ZSTD_DISPONIBLE:
            self.skipTest("un decodeur zstd est installe")
        with self.assertRaises(cmp.CompressionNonSupportee):
            cmp.decompress(cmp.ZIO_COMPRESS_ZSTD, b"\x00" * 32, 4096)

    def test_algorithme_inconnu(self):
        with self.assertRaises(cmp.CompressionNonSupportee):
            cmp.decompress(99, b"\x00" * 32, 4096)

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_lz4_reel_compare_au_fichier_d_origine(self):
        """
        Blocs lz4 reels du dataset docs, decompresses par nous et compares
        au fichier d'origine conserve dans testlab/reference.

        (`zdb -R :d` n'est pas utilise ici : zdb_decompress_block applique un
        byteswap a sa sortie, ce qui en fait un oracle peu commode. La
        comparaison au fichier d'origine est de toute facon plus forte.)
        """
        reference = os.path.join(ROOT, "testlab/reference/docs")
        p = open_pool(IMAGES)
        try:
            from zfsrescue.zpl import ZplReader, build_index
            cible = next(n for n in p.dataset_tree().walk()
                         if n.name == "zrtest/docs")
            objset, _ = p.dmu.read_objset(cible.dataset.bp)
            idx = build_index(ZplReader(p.dmu, objset))
            testes = 0
            for f in sorted(idx.files, key=lambda x: x.object_id):
                chemin_ref = os.path.join(reference, f.path.lstrip("/"))
                if not os.path.exists(chemin_ref):
                    continue
                bp, _etat, _ch = p.dmu.block_pointer_for(f.dnode, 0)
                if bp is None or bp.is_hole or bp.embedded:
                    continue
                if bp.compress_name != "lz4":
                    continue
                res = p.reader.read_block(bp)
                self.assertTrue(res.ok)
                self.assertEqual(len(res.data), bp.lsize)
                with open(chemin_ref, "rb") as fh:
                    attendu = fh.read(bp.lsize)
                self.assertEqual(res.data[:len(attendu)], attendu,
                                 f"decompression lz4 de {f.path}")
                testes += 1
                if testes >= 5:
                    break
            self.assertGreater(testes, 0, "aucun bloc lz4 teste")
        finally:
            p.close()
