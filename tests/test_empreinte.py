"""
Empreinte de controle des supports (scripts/91_empreinte.py).

Le mode « echantillon » doit detecter une ecriture dans les zones surveillees
en ne lisant que quelques dizaines de mega-octets, la ou un sha256 integral
demanderait des heures sur un disque de plusieurs teraoctets.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTIL = os.path.join(ROOT, "scripts", "91_empreinte.py")
TAILLE = 512 << 20          # 512 Mio, creux : l'echantillonnage a du sens


def lancer(mode, chemin):
    r = subprocess.run([sys.executable, OUTIL, "--mode", mode, chemin],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class TestEmpreinte(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "support.img")
        with open(self.img, "wb") as fh:        # creux, avec du contenu aux bords
            fh.truncate(TAILLE)
            fh.seek(0)
            fh.write(os.urandom(1 << 20))
            fh.seek(TAILLE - (1 << 20))
            fh.write(os.urandom(1 << 20))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_echantillon_reproductible(self):
        self.assertEqual(lancer("echantillon", self.img),
                         lancer("echantillon", self.img))

    def test_echantillon_ne_lit_qu_une_partie(self):
        champs = lancer("echantillon", self.img).split()
        lus = int(next(c for c in champs if c.startswith("lus=")).split("=")[1])
        self.assertLess(lus, TAILLE, "le mode echantillon ne lit pas tout")
        self.assertGreater(lus, 1 << 20)

    def test_petit_support_bascule_en_complet(self):
        """Sur un support ou l'echantillon couvrirait tout, autant tout lire."""
        petit = os.path.join(self.tmp, "petit.img")
        with open(petit, "wb") as fh:
            fh.write(os.urandom(8 << 20))
        with open(petit, "rb") as fh:
            attendu = hashlib.sha256(fh.read()).hexdigest()
        champs = lancer("echantillon", petit).split()
        self.assertEqual(champs[1], "complete")
        self.assertEqual(champs[0], attendu)

    def test_complete_egale_sha256(self):
        with open(self.img, "rb") as fh:
            attendu = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(lancer("complete", self.img).split()[0], attendu)

    def test_detecte_une_ecriture(self):
        avant = lancer("echantillon", self.img)
        with open(self.img, "r+b") as fh:          # premier octet = zone label
            fh.seek(0)
            fh.write(b"\xff")
        self.assertNotEqual(avant, lancer("echantillon", self.img),
                            "une ecriture dans une zone surveillee doit changer "
                            "l'empreinte")

    def test_detecte_une_ecriture_en_fin_de_support(self):
        avant = lancer("echantillon", self.img)
        with open(self.img, "r+b") as fh:          # derniere zone de label
            fh.seek(TAILLE - 4096)
            fh.write(b"\xfe" * 16)
        self.assertNotEqual(avant, lancer("echantillon", self.img))

    def test_mode_aucune(self):
        sortie = lancer("aucune", self.img)
        self.assertTrue(sortie.startswith("-  aucune"), sortie)
        self.assertIn("ecriture=fichier", sortie)
