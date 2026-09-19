"""Les tables d'enumerations doivent rester synchronisees avec le source OpenZFS."""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATEUR = os.path.join(ROOT, "scripts", "40_gen_enums.py")


class TestEnumsGeneres(unittest.TestCase):

    @unittest.skipUnless(os.path.exists(GENERATEUR), "generateur absent")
    def test_fichier_a_jour(self):
        r = subprocess.run([sys.executable, GENERATEUR, "--check"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0,
                         "zfsrescue/zfs_enums.py n'est plus synchronise avec "
                         f"les en-tetes OpenZFS : {r.stderr.strip()}")

    def test_valeurs_cles(self):
        from zfsrescue import zfs_enums as E
        self.assertEqual(E.ZIO_CHECKSUM_NAMES[7], "fletcher_4")
        self.assertEqual(E.ZIO_COMPRESS_NAMES[15], "lz4")
        self.assertEqual(E.ZIO_COMPRESS_NAMES[16], "zstd")
        self.assertEqual(E.DMU_OBJECT_TYPE_NAMES[10], "dnode")
        self.assertEqual(E.DMU_OBJECT_TYPE_NAMES[11], "objset")
        self.assertEqual(E.DMU_OBJECT_TYPE_NAMES[19], "plain_file_contents")
        self.assertEqual(E.DMU_OBJECT_TYPE_NAMES[20], "directory_contents")
        self.assertEqual(E.SPA_MINBLOCKSHIFT, 9)
        self.assertEqual(E.DMU_OT_NEWTYPE, 0x80)
