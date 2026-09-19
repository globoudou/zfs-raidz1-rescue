"""
Garantie fondamentale du projet : les sources ne sont JAMAIS modifiees.

Deux niveaux de verification :
  1. audit statique du code (aucun drapeau d'ouverture en ecriture) ;
  2. verification empirique : empreinte SHA-256 et horodatages des images
     avant / apres une execution complete de l'outil.
"""

import glob
import hashlib
import io
import os
import re
import unittest

from zfsrescue.cli import main
from zfsrescue.readonly import ReadOnlyDevice, ReadOnlyError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "zfsrescue")
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))

MOTIFS_INTERDITS = [
    r"\bO_WRONLY\b", r"\bO_RDWR\b", r"\bO_CREAT\b", r"\bO_TRUNC\b",
    r"\bO_APPEND\b", r"\bos\.write\b", r"\bos\.pwrite\b", r"\bos\.truncate\b",
    r"\bos\.ftruncate\b", r"\bos\.unlink\b", r"\bos\.remove\b", r"\bshutil\.",
]


class TestAuditStatique(unittest.TestCase):
    """Aucune primitive d'ecriture ne doit exister dans les modules d'acces."""

    MODULES_ACCES = ("readonly.py", "label.py", "nvlist.py", "topology.py",
                     "consts.py")

    @staticmethod
    def code_sans_commentaires(chemin: str) -> str:
        """Retire commentaires et chaines : l'audit ne porte que sur du code."""
        import io as _io
        import tokenize
        with open(chemin, "rb") as fh:
            jetons = list(tokenize.tokenize(fh.readline))
        garde = [t.string for t in jetons
                 if t.type not in (tokenize.COMMENT, tokenize.STRING)]
        return " ".join(garde)

    def test_aucune_primitive_d_ecriture(self):
        for nom in self.MODULES_ACCES:
            chemin = os.path.join(PKG, nom)
            code = self.code_sans_commentaires(chemin)
            for motif in MOTIFS_INTERDITS:
                self.assertIsNone(
                    re.search(motif, code),
                    f"{nom} contient une primitive d'ecriture : {motif}")

    def test_ouverture_en_lecture_seule(self):
        with open(os.path.join(PKG, "readonly.py"), encoding="utf-8") as fh:
            code = fh.read()
        self.assertIn("os.O_RDONLY", code)
        # le seul os.open du module doit utiliser flags (= O_RDONLY [| O_NOATIME])
        for appel in re.findall(r"os\.open\(([^)]*)\)", code):
            self.assertRegex(appel, r"flags",
                             "os.open doit utiliser le jeu de drapeaux en lecture seule")

    def test_seules_les_sorties_de_rapport_ecrivent(self):
        """L'ecriture n'est autorisee que pour le fichier de rapport, dans cli.py."""
        with open(os.path.join(PKG, "cli.py"), encoding="utf-8") as fh:
            code = fh.read()
        ouvertures = re.findall(r"open\(([^)]*)\)", code)
        self.assertTrue(ouvertures, "au moins un open() attendu dans cli.py")
        for o in ouvertures:
            en_ecriture = re.search(r"""["'][rbt+]*[wax][rbt+]*["']""", o)
            if en_ecriture:
                self.assertIn("args.json", o,
                              f"ecriture non autorisee : open({o})")
            else:
                # lecture seule : aucun mode d'ecriture demande
                self.assertNotRegex(o, r"""["'].*[wax].*["']""",
                                    f"mode d'ouverture suspect : open({o})")


@unittest.skipUnless(IMAGES, "pool de test absent")
class TestNonModificationDesSources(unittest.TestCase):

    @staticmethod
    def empreintes():
        etat = {}
        for img in IMAGES:
            h = hashlib.sha256()
            with open(img, "rb") as fh:
                for bloc in iter(lambda: fh.read(1 << 20), b""):
                    h.update(bloc)
            st = os.stat(img)
            etat[img] = (h.hexdigest(), st.st_mtime_ns, st.st_ctime_ns,
                         st.st_size, st.st_mode)
        return etat

    def test_execution_complete_ne_modifie_rien(self):
        avant = self.empreintes()
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["labels", *IMAGES])
        self.assertEqual(rc, 0)
        apres = self.empreintes()
        self.assertEqual(avant, apres,
                         "les images sources doivent etre bit-a-bit identiques")

    def test_images_en_lecture_seule_sur_le_disque(self):
        for img in IMAGES:
            mode = os.stat(img).st_mode & 0o222
            self.assertEqual(mode, 0, f"{img} devrait etre en mode 444")


class TestBackendLectureSeule(unittest.TestCase):
    def test_refuse_un_repertoire(self):
        with self.assertRaises(ReadOnlyError):
            ReadOnlyDevice(ROOT)

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_refuse_une_lecture_hors_support(self):
        with ReadOnlyDevice(IMAGES[0]) as dev:
            with self.assertRaises(ReadOnlyError):
                dev.pread(dev.info.size - 10, 100)
            with self.assertRaises(ReadOnlyError):
                dev.pread(-1, 10)

    @unittest.skipUnless(IMAGES, "pool de test absent")
    def test_pread_est_exact(self):
        with ReadOnlyDevice(IMAGES[0]) as dev:
            a = dev.pread(0, 4096)
            b = dev.pread(0, 4096)
            self.assertEqual(a, b)
            self.assertEqual(len(dev.pread(1 << 20, 7)), 7)
