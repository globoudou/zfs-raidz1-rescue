"""
Etapes 6 a 9 : navigation ZPL, extraction, fichiers partiels, validation.

Principes verifies :
  * avec les 4 disques, l'outil doit restituer les fichiers BIT A BIT ;
  * avec 2 disques, il ne doit JAMAIS declarer recupere un contenu faux ;
  * les attributs reconstitues par hypothese doivent coincider exactement avec
    ceux lus quand la disposition SA est disponible.
"""

import glob
import hashlib
import os
import tempfile
import unittest

from zfsrescue.extract import (ETAT_COMPLET, ETAT_PARTIEL, ETAT_PERDU,
                               ETAT_VIDE, extraire_fichier, extraire_noeuds,
                               resume)
from zfsrescue.pool import open_pool
from zfsrescue.zpl import ZplReader, build_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "testlab/images")
IMAGES = sorted(glob.glob(os.path.join(IMAGES_DIR, "disk?.img")))
REFERENCE = os.path.join(ROOT, "testlab/reference")
SURVIVANTS_ALIGNES = IMAGES[2:]                    # colonnes 2 et 3
SURVIVANTS_DECALES = [IMAGES[0], IMAGES[2]]        # colonnes 0 et 2


def sha256(chemin):
    h = hashlib.sha256()
    with open(chemin, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def index_dataset(pool, nom):
    for n in pool.dataset_tree().walk():
        if n.name != nom or n.dataset is None:
            continue
        objset, res = pool.dmu.read_objset(n.dataset.bp)
        if objset is None:
            return None, None, res.status
        zpl = ZplReader(pool.dmu, objset)
        return zpl, build_index(zpl), None
    return None, None, "dataset absent"


@unittest.skipUnless(len(IMAGES) == 4 and os.path.isdir(REFERENCE),
                     "pool de test absent")
class TestNavigationZpl(unittest.TestCase):

    def test_arborescence_complete_avec_quatre_disques(self):
        p = open_pool(IMAGES)
        try:
            for ds, sous_dossier in (("zrtest/small", "small"),
                                     ("zrtest/docs", "docs"),
                                     ("zrtest/edge", "edge")):
                zpl, idx, echec = index_dataset(p, ds)
                self.assertIsNone(echec)
                with self.subTest(dataset=ds):
                    attendus = set()
                    base = os.path.join(REFERENCE, sous_dossier)
                    for rep, _s, fichiers in os.walk(base):
                        for f in fichiers:
                            attendus.add("/" + os.path.relpath(
                                os.path.join(rep, f), base))
                    trouves = {n.path for n in idx.files}
                    self.assertEqual(trouves, attendus,
                                     "tous les fichiers doivent etre listes")
                    for n in idx.files:
                        self.assertEqual(
                            n.size,
                            os.path.getsize(os.path.join(base, n.path.lstrip("/"))),
                            f"taille de {n.path}")
        finally:
            p.close()

    def test_attributs_infereres_identiques_aux_attributs_lus(self):
        """
        Avec 4 disques la disposition SA est lisible ; avec 2 elle ne l'est pas
        et l'outil doit l'inferer. Les valeurs doivent etre IDENTIQUES.
        """
        p4 = open_pool(IMAGES)
        p2 = open_pool(SURVIVANTS_ALIGNES)
        try:
            _z4, i4, _ = index_dataset(p4, "zrtest/docs")
            z2, i2, _ = index_dataset(p2, "zrtest/docs")
            self.assertGreater(z2.inferred_layouts, 0,
                               "la disposition SA doit bien etre inferee ici")
            communs = 0
            for oid, n2 in i2.nodes.items():
                n4 = i4.nodes.get(oid)
                if n4 is None or not n2.attrs or not n4.attrs:
                    continue
                communs += 1
                for cle in ("ZPL_SIZE", "ZPL_MODE", "ZPL_LINKS", "ZPL_UID",
                            "ZPL_GID", "ZPL_PARENT", "ZPL_MTIME", "ZPL_CRTIME"):
                    self.assertEqual(n2.attrs.get(cle), n4.attrs.get(cle),
                                     f"objet {oid}, attribut {cle}")
                self.assertTrue(n2.attrs_inferred)
                self.assertFalse(n4.attrs_inferred)
            self.assertGreater(communs, 20)
        finally:
            p4.close()
            p2.close()

    def test_racine_deduite_quand_le_noeud_maitre_manque(self):
        p = open_pool(SURVIVANTS_ALIGNES)
        try:
            zpl, idx, _ = index_dataset(p, "zrtest/small")
            self.assertIsNone(zpl.root_object,
                              "le noeud maitre est cense etre illisible ici")
            self.assertIsNotNone(idx.root_object,
                                 "la racine doit etre deduite du parent de soi-meme")
            self.assertTrue(any("racine deduite" in e for e in idx.errors))
            for n in idx.files:
                self.assertFalse(n.path.startswith("/(parent perdu)"),
                                 f"chemin non reconstruit : {n.path}")
        finally:
            p.close()


@unittest.skipUnless(len(IMAGES) == 4 and os.path.isdir(REFERENCE),
                     "pool de test absent")
class TestExtraction(unittest.TestCase):

    def _extraire(self, images, dataset, dossier_ref, dest=None, **kw):
        p = open_pool(images)
        try:
            _zpl, idx, echec = index_dataset(p, dataset)
            self.assertIsNone(echec)
            res = extraire_noeuds(p.dmu, sorted(idx.files, key=lambda n: n.path),
                                  dest, **kw)
            return res
        finally:
            p.close()

    def test_extraction_bit_a_bit_avec_quatre_disques(self):
        with tempfile.TemporaryDirectory() as d:
            for dataset, sous in (("zrtest/small", "small"),
                                  ("zrtest/edge", "edge"),
                                  ("zrtest/docs", "docs")):
                res = self._extraire(IMAGES, dataset, sous, d)
                with self.subTest(dataset=dataset):
                    self.assertTrue(res)
                    for r in res:
                        # un fichier de taille nulle a son propre etat
                        self.assertIn(r.state, (ETAT_COMPLET, ETAT_VIDE), r.path)
                        ref = os.path.join(REFERENCE, sous, r.path.lstrip("/"))
                        self.assertEqual(r.sha256, sha256(ref),
                                         f"{r.path} doit etre identique a l'original")
                        self.assertEqual(sha256(r.written_to), sha256(ref))
                        self.assertEqual(os.path.getsize(r.written_to),
                                         os.path.getsize(ref))

    def test_aucune_fausse_declaration_avec_deux_disques(self):
        """Regle fondamentale : ce qui est declare recupere doit etre exact."""
        for images in (SURVIVANTS_ALIGNES, SURVIVANTS_DECALES):
            with self.subTest(images=[os.path.basename(i) for i in images]):
                with tempfile.TemporaryDirectory() as d:
                    res = self._extraire(images, "zrtest/small", "small", d)
                    complets = partiels = 0
                    for r in res:
                        ref = os.path.join(REFERENCE, "small",
                                           r.path.lstrip("/"))
                        if not os.path.exists(ref):
                            continue
                        if r.state == ETAT_COMPLET:
                            complets += 1
                            self.assertEqual(r.sha256, sha256(ref),
                                             f"{r.path} declare complet mais different")
                        elif r.state == ETAT_PARTIEL:
                            partiels += 1
                            with open(ref, "rb") as a, open(r.written_to, "rb") as b:
                                orig, sortie = a.read(), b.read()
                            for bl in r.blocks:
                                if bl.status.startswith("RECOVERED"):
                                    self.assertEqual(
                                        orig[bl.offset:bl.offset + bl.length],
                                        sortie[bl.offset:bl.offset + bl.length],
                                        f"{r.path} bloc {bl.blkid}")
                    self.assertGreater(complets, 0)

    def test_reconstruction_par_parite_effective(self):
        """La paire decalee doit declencher des reconstructions XOR valides."""
        res = self._extraire(SURVIVANTS_DECALES, "zrtest/small", "small")
        r = resume(res)
        self.assertGreater(r["blocks_reconstructed"], 0,
                           "des blocs doivent etre reconstruits par la parite")
        self.assertEqual(r["by_state"].get(ETAT_COMPLET, 0)
                         + r["by_state"].get(ETAT_VIDE, 0), len(res))

    def test_option_sans_comblement(self):
        """--no-fill arrete le fichier au premier bloc manquant."""
        p = open_pool(SURVIVANTS_ALIGNES)
        try:
            _z, idx, _ = index_dataset(p, "zrtest/docs")
            partiels = []
            for n in sorted(idx.files, key=lambda x: x.path):
                r = extraire_fichier(p.dmu, n.dnode, None, n.size,
                                     n.object_id, n.path, combler=False,
                                     size_inferred=n.attrs_inferred)
                if r.state == ETAT_PARTIEL:
                    partiels.append(r)
            for r in partiels:
                self.assertTrue(any("interrompue" in e for e in r.errors))
        finally:
            p.close()

    def test_fichier_totalement_perdu_n_est_pas_declare_recupere(self):
        res = self._extraire(SURVIVANTS_ALIGNES, "zrtest/small", "small")
        perdus = [r for r in res if r.state == ETAT_PERDU]
        self.assertGreater(len(perdus), 0)
        for r in perdus:
            self.assertEqual(r.bytes_recovered, 0)
            self.assertEqual(r.blocks_ok, 0)

    def test_fichier_vide_a_une_empreinte_meme_sans_ecriture(self):
        """Regression : en analyse seule, un fichier vide doit porter son sha256."""
        import hashlib as _h
        res = self._extraire(IMAGES, "zrtest/edge", "edge")
        vide = next(r for r in res if r.path == "/vide.bin")
        self.assertEqual(vide.state, ETAT_VIDE)
        self.assertEqual(vide.sha256, _h.sha256(b"").hexdigest())
        self.assertIsNone(vide.written_to)

    def test_cas_limites(self):
        """Fichier vide, 1 octet, exactement 128 Kio, 128 Kio + 1."""
        with tempfile.TemporaryDirectory() as d:
            res = {r.path: r for r in
                   self._extraire(IMAGES, "zrtest/edge", "edge", d)}
            for nom, taille in (("/vide.bin", 0), ("/un_octet.bin", 1),
                                ("/exact_128K.bin", 128 * 1024),
                                ("/exact_128K_plus_1.bin", 128 * 1024 + 1)):
                with self.subTest(fichier=nom):
                    r = res[nom]
                    self.assertEqual(r.size, taille)
                    self.assertEqual(os.path.getsize(r.written_to), taille)
                    self.assertEqual(
                        sha256(r.written_to),
                        sha256(os.path.join(REFERENCE, "edge", nom.lstrip("/"))))


@unittest.skipUnless(len(IMAGES) == 4 and os.path.isdir(REFERENCE),
                     "pool de test absent")
class TestRobustesseExtraction(unittest.TestCase):
    """
    Une anomalie sur un fichier ne doit jamais interrompre une extraction qui
    peut durer des heures. Cas rencontre sur de vraies donnees : un objet
    designe comme parent un autre objet qui est un fichier, si bien que le
    chemin reconstitue est impossible a creer.
    """

    def _un_fichier(self, pool, dataset="zrtest/docs"):
        _zpl, idx, echec = index_dataset(pool, dataset)
        self.assertIsNone(echec)
        return sorted(idx.files, key=lambda n: n.path)[0]

    def test_chemin_impossible_ecrit_a_plat_sans_planter(self):
        import copy
        p = open_pool(IMAGES)
        try:
            modele = self._un_fichier(p)
            with tempfile.TemporaryDirectory() as d:
                # un fichier existe deja la ou il faudrait un repertoire
                os.makedirs(os.path.join(d, "ds"), exist_ok=True)
                with open(os.path.join(d, "ds", "objet_10"), "wb") as fh:
                    fh.write(b"x")
                noeud = copy.copy(modele)
                noeud.path = "/objet_10/objet_11"
                res = extraire_noeuds(p.dmu, [noeud], d, prefixe="ds")
                self.assertEqual(len(res), 1)
                r = res[0]
                self.assertTrue(any("_conflits" in e for e in r.errors), r.errors)
                self.assertIsNotNone(r.written_to)
                self.assertTrue(os.path.exists(r.written_to))
                self.assertEqual(r.state, ETAT_COMPLET)
        finally:
            p.close()

    def test_une_anomalie_n_interrompt_pas_le_lot(self):
        import copy
        p = open_pool(IMAGES)
        try:
            modele = self._un_fichier(p)
            with tempfile.TemporaryDirectory() as d:
                os.makedirs(os.path.join(d, "ds"), exist_ok=True)
                with open(os.path.join(d, "ds", "objet_10"), "wb") as fh:
                    fh.write(b"x")
                mauvais = copy.copy(modele)
                mauvais.path = "/objet_10/impossible"
                bon1, bon2 = copy.copy(modele), copy.copy(modele)
                bon1.path, bon2.path = "/avant.bin", "/apres.bin"
                res = extraire_noeuds(p.dmu, [bon1, mauvais, bon2], d,
                                      prefixe="ds")
                self.assertEqual(len(res), 3, "aucun fichier ne doit etre perdu "
                                              "de vue")
                self.assertTrue(os.path.exists(os.path.join(d, "ds", "avant.bin")))
                self.assertTrue(os.path.exists(os.path.join(d, "ds", "apres.bin")))
        finally:
            p.close()

    def test_parent_qui_n_est_pas_un_repertoire_est_rattache_aux_orphelins(self):
        """La reconstruction de chemins ne doit pas fabriquer d'arborescence
        impossible."""
        from zfsrescue.zpl import ZplReader, build_index
        p = open_pool(IMAGES)
        try:
            zpl, idx, _ = index_dataset(p, "zrtest/docs")
            fichiers = [n for n in idx.nodes.values() if n.is_file]
            victime = fichiers[0]
            faux_parent = fichiers[1]
            victime.parent_obj = faux_parent.object_id      # un fichier !
            idx2 = build_index(zpl)
            for n in idx2.nodes.values():
                parent = idx2.nodes.get(n.parent_obj) if n.parent_obj else None
                if parent is not None and not parent.is_dir:
                    self.assertIn("(parent perdu)", n.path,
                                  "un fichier ne peut pas servir de repertoire")
        finally:
            p.close()


@unittest.skipUnless(len(IMAGES) == 4 and os.path.isdir(REFERENCE),
                     "pool de test absent")
class TestRangementDesResultats(unittest.TestCase):
    """
    Un fichier dont aucun bloc n'est lisible serait ecrit entierement nul :
    meme nom, meme taille, rien pour le distinguer d'un fichier valide tant
    qu'on ne l'ouvre pas. Il ne doit pas etre ecrit du tout, et les fichiers
    partiels doivent etre ranges a part.
    """

    def _extraire(self, dest, **kw):
        p = open_pool(SURVIVANTS_ALIGNES)
        try:
            _z, idx, echec = index_dataset(p, "zrtest/docs")
            self.assertIsNone(echec)
            return extraire_noeuds(p.dmu,
                                   sorted(idx.files, key=lambda n: n.path),
                                   dest, prefixe="docs", **kw)
        finally:
            p.close()

    def test_fichier_perdu_non_ecrit(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._extraire(d)
            perdus = [r for r in res if r.state == ETAT_PERDU]
            self.assertTrue(perdus, "ce scenario perd des fichiers")
            for r in perdus:
                self.assertIsNone(r.written_to)
                self.assertTrue(any("entierement nul" in e for e in r.errors))
            # aucun fichier nul ne traine sur le disque
            for rep, _s, fichiers in os.walk(d):
                for f in fichiers:
                    chemin = os.path.join(rep, f)
                    with open(chemin, "rb") as fh:
                        contenu = fh.read()
                    self.assertTrue(contenu == b"" or contenu.count(0) != len(contenu),
                                    f"{chemin} est entierement nul")

    def test_option_ecrire_perdus(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._extraire(d, ecrire_perdus=True)
            perdus = [r for r in res if r.state == ETAT_PERDU]
            self.assertTrue(perdus)
            for r in perdus:
                self.assertIsNotNone(r.written_to)
                with open(r.written_to, "rb") as fh:
                    contenu = fh.read()
                self.assertEqual(contenu.count(0), len(contenu),
                                 "un fichier perdu ecrit est nul, par construction")

    def test_partiels_ranges_a_part(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._extraire(d)
            partiels = [r for r in res if r.state == ETAT_PARTIEL]
            self.assertTrue(partiels, "ce scenario a des fichiers partiels")
            for r in partiels:
                self.assertIn("_partiels", r.written_to)
            # l'arborescence principale ne contient que des fichiers exacts
            principale = os.path.join(d, "docs")
            for rep, _s, fichiers in os.walk(principale):
                for f in fichiers:
                    chemin = os.path.join(rep, f)
                    rel = os.path.relpath(chemin, principale)
                    ref = os.path.join(REFERENCE, "docs", rel)
                    self.assertTrue(os.path.exists(ref), rel)
                    self.assertEqual(sha256(chemin), sha256(ref),
                                     f"{rel} doit etre identique a l'original")

    def test_une_seule_lecture_par_fichier(self):
        """Le rangement se fait apres coup : pas de passe de sonde supplementaire."""
        with tempfile.TemporaryDirectory() as d:
            p = open_pool(SURVIVANTS_ALIGNES)
            try:
                _z, idx, _e = index_dataset(p, "zrtest/docs")
                fichiers = sorted(idx.files, key=lambda n: n.path)[:5]
                avant = p.dmu.cache_misses + p.dmu.cache_hits
                extraire_noeuds(p.dmu, fichiers, d, prefixe="docs")
                lectures = p.dmu.cache_misses + p.dmu.cache_hits - avant
                blocs = sum(f.dnode.nblocks for f in fichiers)
                self.assertLessEqual(lectures, blocs * 2 + 10,
                                     "chaque bloc ne doit etre lu qu'une fois "
                                     "(plus les blocs indirects)")
            finally:
                p.close()

    def test_repertoire_de_travail_nettoye(self):
        with tempfile.TemporaryDirectory() as d:
            self._extraire(d)
            self.assertFalse(os.path.exists(os.path.join(d, "_en_cours")),
                             "le repertoire temporaire doit disparaitre")
