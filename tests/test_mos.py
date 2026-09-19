"""
Etape 5 — lecture du MOS (et debut de l'etape 6 : hierarchie des datasets).

Oracle : `zdb -dddd` et `zdb -d`. Verifie aussi que le resultat obtenu avec
2 disques est identique a celui obtenu avec les 4.
"""

import glob
import os
import unittest

from zfsrescue.blockio import MISSING_DATA, RECOVERED
from zfsrescue.dmu import parse_objset
from zfsrescue.pool import open_pool
from zfsrescue.zap import read_zap

from . import oracle_zdb as Z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "testlab/images")
IMAGES = sorted(glob.glob(os.path.join(IMAGES_DIR, "disk?.img")))
SURVIVANTS = IMAGES[2:]          # diskc + diskd  (colonnes 2 et 3)

# Correspondance entre les noms de type de zdb et les notres
TYPES_ZDB = {
    "DMU dnode": "dnode",
    "object directory": "object_directory",
    "zap": "otn:zap,metadata",
    "DSL props": "dsl_props",
    "DSL directory": "dsl_dir",
    "DSL directory child map": "dsl_dir_child_map",
    "DSL dataset": "dsl_dataset",
    "DSL dataset snap map": "dsl_ds_snap_map",
    "DSL deadlist map": "deadlist",
    "bpobj": "bpobj",
    "SPA space map": "space_map",
    "SPA history": "spa_history",
    "packed nvlist": "packed_nvlist",
    "object array": "object_array",
    "DSL permissions": "dsl_perms",
    "DSL clones": "dsl_clones",
    "next clones": "next_clones",
    "pool props": "pool_props",
    "bpobj subobj": "bpobj_subobj",
    "uint64": "otn:uint64,metadata",
}


@unittest.skipUnless(len(IMAGES) == 4, "pool de test absent")
class TestLectureDuMos(unittest.TestCase):

    def test_mos_lisible_avec_deux_disques(self):
        p = open_pool(SURVIVANTS)
        try:
            self.assertEqual(p.mos.type_name, "meta (MOS)")
            self.assertEqual(sorted(p.reader.devices), [2, 3])
            self.assertEqual(p.mos.meta_dnode.type_name, "dnode")
        finally:
            p.close()

    def test_tous_les_objets_du_mos_sont_lus(self):
        p = open_pool(SURVIVANTS)
        try:
            alloues, illisibles = [], 0
            for oid, dn, _etat in p.dmu.iter_dnodes(p.mos.meta_dnode):
                if dn is None:
                    illisibles += 1
                elif dn.allocated:
                    alloues.append(oid)
            self.assertEqual(illisibles, 0,
                             "aucun dnode du MOS ne devrait manquer "
                             "(3 copies des metadonnees)")
            self.assertEqual(len(alloues), p.uberblock.rootbp.fill,
                             "le nombre d'objets doit egaler le champ fill")
        finally:
            p.close()

    @unittest.skipUnless(Z.available, "zdb indisponible")
    def test_objets_identiques_a_zdb(self):
        ref = Z.objects("zrtest", IMAGES_DIR, section="mos")
        p = open_pool(SURVIVANTS)
        try:
            vus = set()
            for oid, dn, _etat in p.dmu.iter_dnodes(p.mos.meta_dnode):
                if dn is None or not dn.allocated or oid not in ref:
                    continue
                r = ref[oid]
                vus.add(oid)
                with self.subTest(objet=oid):
                    self.assertEqual(dn.nlevels, r["levels"])
                    self.assertEqual(dn.datablksize, r["dblk"])
                    self.assertEqual(dn.indblksize, r["iblk"])
                    self.assertEqual(dn.dnode_size, r["dnsize"])
                    if r["maxblkid"] is not None:
                        self.assertEqual(dn.maxblkid, r["maxblkid"])
                    if r.get("bonuslen"):
                        self.assertEqual(dn.bonuslen, r["bonuslen"])
                    attendu = TYPES_ZDB.get(r["type"])
                    if attendu:
                        self.assertEqual(dn.type_name, attendu)
            # objet 0 = le meta-dnode lui-meme, non liste par iter_dnodes
            self.assertEqual(len(vus), len(ref) - 1)
        finally:
            p.close()

    def test_annuaire_des_objets(self):
        p = open_pool(SURVIVANTS)
        try:
            dn, _ = p.dmu.read_dnode(p.mos.meta_dnode, 1)
            z = read_zap(p.dmu, dn)
            self.assertEqual(z.kind, "fat")
            self.assertTrue(z.complete)
            self.assertEqual(len(z.entries), z.declared_entries)
            for cle in ("root_dataset", "config", "features_for_read",
                        "sync_bplist", "free_bpobj"):
                self.assertIsNotNone(z.get(cle), f"entree {cle} attendue")
        finally:
            p.close()

    @unittest.skipUnless(Z.available, "zdb indisponible")
    def test_arborescence_des_datasets_identique_a_zdb(self):
        ref = Z.datasets("zrtest", IMAGES_DIR)
        p = open_pool(SURVIVANTS)
        try:
            arbre = p.dataset_tree()
            trouves = {}
            for n in arbre.walk():
                if n.dataset is not None:
                    trouves[n.name] = n.dataset
            for nom, r in ref.items():
                if "@" in nom or nom == "mos":
                    continue
                with self.subTest(dataset=nom):
                    self.assertIn(nom, trouves)
                    self.assertEqual(trouves[nom].object_id, r["id"])
                    self.assertEqual(trouves[nom].creation_txg, r["cr_txg"])
        finally:
            p.close()

    @unittest.skipUnless(Z.available, "zdb indisponible")
    def test_snapshots_retrouves(self):
        ref = {n: r for n, r in Z.datasets("zrtest", IMAGES_DIR).items()
               if "@" in n}
        p = open_pool(SURVIVANTS)
        try:
            trouves = {}
            for n in p.dataset_tree().walk():
                for nom, obj in n.snapshots:
                    trouves[f"{n.name}@{nom}"] = obj
            for nom, r in ref.items():
                with self.subTest(snapshot=nom):
                    self.assertEqual(trouves.get(nom), r["id"])
        finally:
            p.close()

    def test_deux_disques_donnent_le_meme_resultat_que_quatre(self):
        p2 = open_pool(SURVIVANTS)
        p4 = open_pool(IMAGES)
        try:
            self.assertEqual(p2.uberblock.identity, p4.uberblock.identity)
            a2 = {n.name: (n.dir_obj,
                           n.dataset.object_id if n.dataset else None)
                  for n in p2.dataset_tree().walk()}
            a4 = {n.name: (n.dir_obj,
                           n.dataset.object_id if n.dataset else None)
                  for n in p4.dataset_tree().walk()}
            self.assertEqual(a2, a4)
        finally:
            p2.close()
            p4.close()

    def test_etat_historique_choisi_par_txg(self):
        p = open_pool(SURVIVANTS, txg=32)
        try:
            self.assertEqual(p.uberblock.txg, 32)
            self.assertEqual(p.mos.type_name, "meta (MOS)")
        finally:
            p.close()


@unittest.skipUnless(len(IMAGES) == 4, "pool de test absent")
class TestLectureDeBlocs(unittest.TestCase):

    def test_bloc_sur_colonnes_absentes_est_declare_manquant(self):
        """Un bloc dont les donnees sont sur les disques perdus ne doit jamais
        etre 'reconstitue' : il est declare MISSING_DATA."""
        p2 = open_pool(SURVIVANTS)
        p4 = open_pool(IMAGES)
        try:
            perdus = lisibles = 0
            for oid, dn, _e in p4.dmu.iter_dnodes(p4.mos.meta_dnode):
                if dn is None or not dn.allocated:
                    continue
                for bp in dn.blkptrs:
                    if bp.is_hole or bp.embedded:
                        continue
                    r2 = p2.reader.read_block(bp)
                    r4 = p4.reader.read_block(bp)
                    self.assertEqual(r4.status, RECOVERED,
                                     "avec 4 disques tout doit etre lisible")
                    if r2.ok:
                        lisibles += 1
                        self.assertEqual(r2.data, r4.data,
                                         "les donnees lues doivent etre identiques")
                    else:
                        perdus += 1
                        self.assertEqual(r2.status, MISSING_DATA)
                        self.assertIsNone(r2.data)
            self.assertGreater(lisibles, 0)
        finally:
            p2.close()
            p4.close()

    def test_objset_du_mos_verifie_par_checksum(self):
        p = open_pool(SURVIVANTS)
        try:
            res = p.reader.read_block(p.uberblock.rootbp)
            self.assertTrue(res.ok)
            self.assertTrue(res.checksum_ok)
            self.assertEqual(len(res.data), p.uberblock.rootbp.lsize)
            parse_objset(res.data)
        finally:
            p.close()
