"""
Balayage DSL : retrouver les datasets sans suivre la hierarchie.

Situation rencontree sur de vraies donnees : le ZAP des datasets enfants du
repertoire racine est perdu, donc la descente nominale ne montre qu'un seul
dataset — alors que les objets `dsl_dataset` du MOS, qui portent le pointeur
vers l'objset de chaque dataset, restent lisibles.
"""

import glob
import json
import os
import tempfile
import unittest

from zfsrescue.cli import main
from zfsrescue.dsl import DslDir, DslReader
from zfsrescue.pool import open_pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = sorted(glob.glob(os.path.join(ROOT, "testlab/images/disk?.img")))
SURVIVANTS = IMAGES[2:]


@unittest.skipUnless(len(IMAGES) == 4, "pool de test absent")
class TestBalayageDsl(unittest.TestCase):

    def test_trouve_tous_les_datasets_de_l_arborescence(self):
        p = open_pool(IMAGES)
        try:
            scan = DslReader(p.dmu, p.mos).scan(p.topology.name)
            par_arbre = {n.dataset.object_id for n in p.dataset_tree().walk()
                         if n.dataset is not None}
            trouves = {e.object_id for e in scan.entries}
            self.assertTrue(par_arbre <= trouves,
                            "le balayage doit au moins retrouver ce que donne "
                            f"la hierarchie ({par_arbre - trouves} manquants)")
            noms = {e.name for e in scan.entries}
            for attendu in ("zrtest", "zrtest/small", "zrtest/docs",
                            "zrtest/big", "zrtest/blobs", "zrtest/edge"):
                self.assertIn(attendu, noms)
        finally:
            p.close()

    def test_snapshots_retrouves_comme_sources(self):
        p = open_pool(IMAGES)
        try:
            scan = DslReader(p.dmu, p.mos).scan(p.topology.name)
            snaps = [e for e in scan.entries if e.kind == "snapshot"]
            self.assertGreaterEqual(len(snaps), 5)
            self.assertIn("zrtest/small@base", {e.name for e in snaps})
            for s in snaps:
                self.assertTrue(s.named)
                self.assertIn("@", s.name)
        finally:
            p.close()

    def test_datasets_retrouves_meme_sans_les_listes_d_enfants(self):
        """
        On simule la panne constatee : tous les ZAP de datasets enfants sont
        illisibles. Les datasets doivent rester trouvables, avec un nom
        synthetique et un objset teste normalement.
        """
        p = open_pool(IMAGES)
        try:
            reference = DslReader(p.dmu, p.mos).scan(p.topology.name)

            cibles = set()
            for oid, dn, _e in p.dmu.iter_dnodes(p.mos.meta_dnode):
                if dn is None or not dn.allocated or not dn.bonus:
                    continue
                if dn.bonustype_name == "dsl_dir":
                    d = DslDir.from_bonus(oid, dn.bonus)
                    if d.child_dir_zapobj:
                        cibles.add(d.child_dir_zapobj)
            self.assertTrue(cibles, "le pool de test doit avoir des child maps")

            vrai = DslReader._dnode

            def casse(self, obj):
                return None if obj in cibles else vrai(self, obj)

            DslReader._dnode = casse
            try:
                degrade = DslReader(p.dmu, p.mos).scan(p.topology.name)
            finally:
                DslReader._dnode = vrai

            self.assertEqual({e.object_id for e in degrade.entries},
                             {e.object_id for e in reference.entries},
                             "les datasets doivent rester trouvables")
            self.assertGreater(degrade.summary()["child_maps_lost"], 0)
            anonymes = [e for e in degrade.entries if not e.named]
            self.assertGreater(len(anonymes), 3,
                               "les noms ne peuvent plus etre reconstitues")
            for e in anonymes:
                self.assertTrue(e.name.startswith("dataset_obj"))
            # les objsets restent testes a l'identique
            etats_ref = {e.object_id: e.objset_status for e in reference.entries}
            for e in degrade.entries:
                self.assertEqual(e.objset_status, etats_ref[e.object_id])
        finally:
            p.close()

    def test_meme_resultat_avec_deux_disques(self):
        p2 = open_pool(SURVIVANTS)
        p4 = open_pool(IMAGES)
        try:
            s2 = DslReader(p2.dmu, p2.mos).scan(p2.topology.name)
            s4 = DslReader(p4.dmu, p4.mos).scan(p4.topology.name)
            self.assertEqual({e.object_id for e in s2.entries},
                             {e.object_id for e in s4.entries},
                             "les metadonnees du MOS ont 3 copies : le balayage "
                             "doit donner la meme liste")
        finally:
            p2.close()
            p4.close()

    def test_objsets_illisibles_signales_sans_etre_devines(self):
        p = open_pool(SURVIVANTS)
        try:
            scan = DslReader(p.dmu, p.mos).scan(p.topology.name)
            perdus = [e for e in scan.entries if not e.usable]
            self.assertTrue(perdus, "ce scenario perd des objsets")
            for e in perdus:
                self.assertIsNone(e.objset)
                self.assertIn(e.objset_status,
                              ("MISSING_DATA", "INVALID_CHECKSUM", "UNKNOWN",
                               "TROU"))
        finally:
            p.close()


@unittest.skipUnless(len(IMAGES) == 4, "pool de test absent")
class TestCliScan(unittest.TestCase):

    def test_commande_datasets(self):
        with tempfile.TemporaryDirectory() as d:
            sortie = os.path.join(d, "datasets.json")
            code = main(["datasets", *SURVIVANTS, "--json", sortie])
            self.assertEqual(code, 0)
            with open(sortie, encoding="utf-8") as fh:
                rapport = json.load(fh)
            s = rapport["scan"]
            self.assertGreaterEqual(s["datasets_found"], 10)
            self.assertGreaterEqual(s["snapshots"], 5)
            self.assertGreaterEqual(s["with_readable_objset"], 1)

    def test_ls_scan_inclut_les_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            sortie = os.path.join(d, "ls.json")
            code = main(["ls", *SURVIVANTS, "--scan", "-q", "--json", sortie])
            self.assertEqual(code, 0)
            with open(sortie, encoding="utf-8") as fh:
                rapport = json.load(fh)
            noms = [x["name"] for x in rapport["datasets"]]
            self.assertTrue(any("@" in n for n in noms),
                            "les snapshots doivent apparaitre")
            self.assertIn("zrtest/docs", noms)

    def test_extract_scan_sur_un_snapshot(self):
        """Un snapshot doit pouvoir servir de source d'extraction."""
        with tempfile.TemporaryDirectory() as d:
            sortie = os.path.join(d, "extract.json")
            code = main(["extract", *IMAGES, "--scan", "--dataset",
                         "zrtest/docs@base", "--dry-run", "--json", sortie])
            self.assertEqual(code, 0)
            with open(sortie, encoding="utf-8") as fh:
                rapport = json.load(fh)
            self.assertEqual(len(rapport["datasets"]), 1)
            ds = rapport["datasets"][0]
            self.assertEqual(ds["name"], "zrtest/docs@base")
            self.assertGreater(ds["summary"]["files_total"], 40)
            self.assertEqual(ds["summary"]["by_state"].get("COMPLET"),
                             ds["summary"]["files_total"])
