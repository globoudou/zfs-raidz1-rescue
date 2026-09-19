"""
Etape 3 — mapping RAIDZ.

Trois niveaux de validation :
  1. valeurs calculees a la main sur des cas de reference ;
  2. invariants du format verifies sur un large balayage de parametres ;
  3. comparaison differentielle avec `zdb -R` : les octets assembles par notre
     mapping doivent etre identiques a ceux qu'OpenZFS restitue.
"""

import glob
import itertools
import os
import subprocess
import unittest

from zfsrescue import consts
from zfsrescue.raidz import (ROLE_DATA, ROLE_PARITY, ROLE_SKIP, io_size_for_psize,
                             STATUS_COMPLETE, STATUS_MISSING_DATA,
                             STATUS_RECONSTRUCTIBLE, analyse_availability,
                             assemble_data, raidz_asize, raidz_map_alloc,
                             read_columns)
from zfsrescue.readonly import ReadOnlyDevice

from . import oracle_zdb as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))
SECTEUR = 4096


class TestValeursDeReference(unittest.TestCase):
    """Cas calcules a la main depuis vdev_raidz.c:585."""

    def test_un_secteur(self):
        """s=1 : 1 colonne de donnees + 1 de parite, rien d'autre n'est touche."""
        rm = raidz_map_alloc(0, SECTEUR, 12, 4, 1)
        self.assertEqual((rm.acols, rm.scols, rm.bigcols, rm.nskip), (2, 2, 2, 0))
        self.assertEqual([(c.devidx, c.role, c.size, c.offset) for c in rm.columns],
                         [(0, ROLE_PARITY, SECTEUR, 0), (1, ROLE_DATA, SECTEUR, 0)])
        self.assertEqual(rm.asize, 2 * SECTEUR)
        self.assertEqual(rm.dva_asize, 2 * SECTEUR)

    def test_deux_secteurs_avec_padding(self):
        """s=2 : 2 donnees + 1 parite = 3 secteurs, arrondis a 4 -> 1 secteur saute."""
        rm = raidz_map_alloc(0, 2 * SECTEUR, 12, 4, 1)
        self.assertEqual((rm.acols, rm.scols, rm.bigcols, rm.nskip), (3, 4, 3, 1))
        roles = [c.role for c in rm.columns]
        self.assertEqual(roles, [ROLE_PARITY, ROLE_DATA, ROLE_DATA, ROLE_SKIP])
        self.assertEqual([c.size for c in rm.columns], [SECTEUR, SECTEUR, SECTEUR, 0])
        self.assertEqual(rm.asize, 3 * SECTEUR)
        self.assertEqual(rm.dva_asize, 4 * SECTEUR)

    def test_stripe_pleine(self):
        """s=3 : la stripe occupe les 4 colonnes, aucun padding."""
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)
        self.assertEqual((rm.acols, rm.scols, rm.bigcols, rm.nskip), (4, 4, 0, 0))
        self.assertEqual([c.devidx for c in rm.columns], [0, 1, 2, 3])
        self.assertTrue(all(c.size == SECTEUR for c in rm.columns))

    def test_rotation_des_colonnes(self):
        """Le bloc demarre en colonne 3 : les colonnes suivantes bouclent avec +1 secteur."""
        rm = raidz_map_alloc(3 * SECTEUR, 3 * SECTEUR, 12, 4, 1)
        self.assertEqual([(c.devidx, c.offset) for c in rm.columns],
                         [(3, 0), (0, SECTEUR), (1, SECTEUR), (2, SECTEUR)])

    def test_permutation_de_parite_tous_les_1_Mio(self):
        """vdev_raidz.c:695 : pour raidz1, les colonnes 0 et 1 sont echangees
        quand le bit 1<<20 de l'offset est arme."""
        sans = raidz_map_alloc(0x40000, SECTEUR, 12, 4, 1)       # bit 20 a 0
        avec = raidz_map_alloc(0x100000, SECTEUR, 12, 4, 1)      # bit 20 a 1
        self.assertEqual([c.devidx for c in sans.columns], [0, 1])
        self.assertEqual([c.devidx for c in avec.columns], [1, 0])
        self.assertEqual(avec.columns[0].role, ROLE_PARITY)
        self.assertEqual(avec.columns[1].role, ROLE_DATA)

    def test_asize_de_reference(self):
        """vdev_raidz_asize() sur des tailles typiques d'enregistrement."""
        # Valeurs confrontees a de vrais blocs du pool de test :
        #   psize 0x20000  -> DVA asize 0x2c000   (zdb : "L0 0:...:2c000 20000L/20000P")
        #   psize 0x100000 -> DVA asize 0x156000  (zdb : "L0 0:...:156000 100000L/100000P")
        cas = {4 << 10: 8 << 10, 8 << 10: 16 << 10, 16 << 10: 24 << 10,
               32 << 10: 48 << 10, 128 << 10: 0x2C000, 1 << 20: 0x156000}
        for psize, attendu in cas.items():
            with self.subTest(psize=psize):
                self.assertEqual(raidz_asize(psize, 12, 4, 1), attendu)


class TestTailleEffectiveDesEs(unittest.TestCase):
    """zio.c:4460 — toute E/S non alignee est arrondie au secteur du vdev."""

    def test_arrondi_au_secteur(self):
        self.assertEqual(io_size_for_psize(512, 12), 4096)
        self.assertEqual(io_size_for_psize(1024, 12), 4096)
        self.assertEqual(io_size_for_psize(4096, 12), 4096)
        self.assertEqual(io_size_for_psize(4097, 12), 8192)
        self.assertEqual(io_size_for_psize(3584, 12), 4096)
        self.assertEqual(io_size_for_psize(512, 9), 512)
        self.assertEqual(io_size_for_psize(513, 9), 1024)

    def test_un_bloc_de_512_octets_occupe_un_secteur(self):
        """Confirme par de vrais blkptr du pool : 200L/... -> DVA asize 2000."""
        rm = raidz_map_alloc(0, io_size_for_psize(512, 12), 12, 4, 1)
        self.assertEqual(rm.dva_asize, 0x2000)

    def test_psize_invalide(self):
        with self.assertRaises(ValueError):
            io_size_for_psize(0, 12)


class TestAlignementDesAllocations(unittest.TestCase):
    """
    Consequence du format : une allocation RAIDZ occupe toujours un multiple de
    (nparity+1) secteurs (roundup(tot, nparity+1) dans vdev_raidz_map_alloc).
    Les blocs commencent donc a un secteur pair sur un raidz1, ce qui a une
    consequence directe sur la recuperabilite : un petit bloc tient entierement
    dans une paire de colonnes {0,1} ou {2,3}, jamais a cheval.

    Verifie sur les vrais blocs du pool de test par scripts/32_dva_stats.py.
    """

    def test_taille_allouee_multiple_de_nparity_plus_1(self):
        for nsec in range(1, 64):
            for nparity, dcols in ((1, 4), (2, 6), (3, 9)):
                rm = raidz_map_alloc(0, nsec * SECTEUR, 12, dcols, nparity)
                self.assertEqual(rm.dva_asize % ((nparity + 1) * SECTEUR), 0)

    def test_petit_bloc_sur_secteur_pair_ne_chevauche_pas_les_paires(self):
        for bsec in range(0, 32, 2):          # secteurs pairs uniquement
            rm = raidz_map_alloc(bsec * SECTEUR, SECTEUR, 12, 4, 1)
            devidx = {c.devidx for c in rm.columns}
            self.assertIn(devidx, ({0, 1}, {2, 3}),
                          "un bloc d'un secteur reste dans une paire de colonnes")


class TestInvariantsDuFormat(unittest.TestCase):
    """Proprietes qui doivent tenir pour toute combinaison de parametres."""

    PARAMS = [(ashift, dcols, nparity)
              for ashift in (9, 12, 13)
              for dcols, nparity in ((3, 1), (4, 1), (5, 1), (8, 1),
                                     (6, 2), (9, 3))]

    def test_invariants(self):
        for ashift, dcols, nparity in self.PARAMS:
            sec = 1 << ashift
            for nsec in list(range(1, 40)) + [64, 100, 128, 255, 256, 300]:
                for bsec in (0, 1, 2, 3, 7, 13, 64, 257, 1024):
                    off = bsec * sec
                    size = nsec * sec
                    with self.subTest(ashift=ashift, dcols=dcols,
                                      nparity=nparity, nsec=nsec, bsec=bsec):
                        rm = raidz_map_alloc(off, size, ashift, dcols, nparity)

                        # bornes
                        self.assertLessEqual(rm.acols, rm.scols)
                        self.assertLessEqual(rm.scols, dcols)
                        self.assertGreaterEqual(rm.acols, 2)

                        # tailles
                        self.assertEqual(sum(c.size for c in rm.columns), rm.asize)
                        self.assertEqual(
                            sum(c.size for c in rm.data_columns), size,
                            "les colonnes de donnees doivent totaliser psize")
                        self.assertEqual(len(rm.parity_columns),
                                         min(nparity, rm.acols))

                        # une colonne physique n'est jamais utilisee deux fois
                        used = [c.devidx for c in rm.columns if c.size > 0]
                        self.assertEqual(len(used), len(set(used)))
                        self.assertTrue(all(0 <= d < dcols for d in used))

                        # offsets : o ou o + un secteur (bouclage)
                        offsets = {c.offset for c in rm.columns}
                        self.assertLessEqual(len(offsets), 2)
                        self.assertLessEqual(max(offsets) - min(offsets), sec)

                        # coherence avec vdev_raidz_asize()
                        self.assertEqual(rm.dva_asize,
                                         raidz_asize(size, ashift, dcols, nparity))
                        self.assertEqual(rm.dva_asize % ((nparity + 1) * sec), 0)

                        # offset physique = offset allouable + 4 Mio
                        for c in rm.columns:
                            self.assertEqual(
                                c.phys_offset,
                                c.offset + consts.VDEV_LABEL_START_SIZE)

    def test_les_tailles_de_colonnes_sont_decroissantes(self):
        """Les colonnes larges viennent en premier : tailles non croissantes."""
        for nsec in range(1, 50):
            rm = raidz_map_alloc(0, nsec * SECTEUR, 12, 4, 1)
            tailles = [c.size for c in rm.columns]
            self.assertEqual(tailles, sorted(tailles, reverse=True))


class TestErreurs(unittest.TestCase):
    def test_offset_non_aligne(self):
        with self.assertRaises(ValueError):
            raidz_map_alloc(1234, SECTEUR, 12, 4, 1)

    def test_taille_non_alignee(self):
        with self.assertRaises(ValueError):
            raidz_map_alloc(0, 1234, 12, 4, 1)

    def test_parametres_absurdes(self):
        with self.assertRaises(ValueError):
            raidz_map_alloc(0, SECTEUR, 12, 1, 1)      # dcols <= nparity
        with self.assertRaises(ValueError):
            raidz_map_alloc(0, SECTEUR, 12, 4, 0)      # nparity nul
        with self.assertRaises(ValueError):
            raidz_map_alloc(0, 0, 12, 4, 1)            # taille nulle


class TestDisponibilite(unittest.TestCase):
    """Verdicts de recuperabilite — aucune donnee n'est jamais supposee."""

    def test_toutes_colonnes_presentes(self):
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)
        av = analyse_availability(rm, [0, 1, 2, 3])
        self.assertEqual(av.status, STATUS_COMPLETE)

    def test_une_colonne_de_donnees_absente(self):
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)   # colonnes 0(P),1,2,3
        av = analyse_availability(rm, [0, 1, 2])          # 3 absente
        self.assertEqual(av.status, STATUS_RECONSTRUCTIBLE)
        self.assertEqual(av.usable_parity, 1)

    def test_parite_absente_mais_donnees_completes(self):
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)
        av = analyse_availability(rm, [1, 2, 3])          # parite (col 0) absente
        self.assertEqual(av.status, STATUS_COMPLETE)
        self.assertEqual(av.usable_parity, 0)

    def test_deux_colonnes_de_donnees_absentes(self):
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)
        av = analyse_availability(rm, [0, 1])
        self.assertEqual(av.status, STATUS_MISSING_DATA)
        self.assertEqual(av.missing_data_columns, [2, 3])

    def test_petit_bloc_entierement_sur_les_survivants(self):
        """Un bloc d'un secteur n'utilise que 2 colonnes : il peut survivre
        alors meme que la moitie du vdev a disparu."""
        trouve = False
        for bsec in range(16):
            rm = raidz_map_alloc(bsec * SECTEUR, SECTEUR, 12, 4, 1)
            av = analyse_availability(rm, [2, 3])
            if av.status == STATUS_COMPLETE:
                trouve = True
                # seules les colonnes de DONNEES doivent etre sur les survivants ;
                # la parite peut manquer sans consequence.
                self.assertTrue(all(c.devidx in (2, 3) for c in rm.data_columns))
        self.assertTrue(trouve, "au moins un bloc doit tenir sur les colonnes 2 et 3")

    def test_stripe_pleine_irrecuperable_avec_deux_disques(self):
        for bsec in range(16):
            rm = raidz_map_alloc(bsec * SECTEUR, 3 * SECTEUR, 12, 4, 1)
            av = analyse_availability(rm, [2, 3])
            self.assertEqual(av.status, STATUS_MISSING_DATA,
                             "une stripe complete ne peut pas etre reconstruite")


@unittest.skipUnless(IMAGES and Z.available, "images de test ou zdb absents")
class TestComparaisonAvecZdb(unittest.TestCase):
    """
    Les octets assembles par notre mapping doivent etre identiques a ceux
    restitues par `zdb -R` (qui utilise le mapping d'OpenZFS).
    """

    TAILLES = [0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x8000, 0x20000, 0x100000]
    OFFSETS = [0x0, 0x1000, 0x2000, 0x3000, 0x100000, 0x101000,
               0x1a16c000, 0x40000000]

    @classmethod
    def setUpClass(cls):
        cls.devs = {i: ReadOnlyDevice(p) for i, p in enumerate(IMAGES)}
        cls.pool_dir = os.path.join(ROOT, "testlab/images")

    @classmethod
    def tearDownClass(cls):
        for d in cls.devs.values():
            d.close()

    def lire_avec_zdb(self, offset: int, size: int) -> bytes:
        return subprocess.run(
            [Z.ZDB, "-e", "-p", self.pool_dir, "-R", "zrtest",
             f"0:{offset:x}:{size:x}:r"],
            capture_output=True, check=True).stdout

    def test_assemblage_identique_a_zdb(self):
        for offset, size in itertools.product(self.OFFSETS, self.TAILLES):
            with self.subTest(offset=hex(offset), size=hex(size)):
                rm = raidz_map_alloc(offset, size, 12, 4, 1)
                nos_octets = assemble_data(rm, read_columns(rm, self.devs))
                self.assertEqual(nos_octets, self.lire_avec_zdb(offset, size))

    def test_colonne_absente_donne_none(self):
        """Sans le support, la colonne vaut None : jamais des zeros."""
        rm = raidz_map_alloc(0, 3 * SECTEUR, 12, 4, 1)
        partiel = {k: v for k, v in self.devs.items() if k in (2, 3)}
        cols = read_columns(rm, partiel)
        self.assertIsNone(cols[1])
        self.assertIsNotNone(cols[2])
        self.assertIsNone(assemble_data(rm, cols))
