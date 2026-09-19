"""
Valide le decodeur nvlist contre la bibliotheque OpenZFS elle-meme :
les octets de reference sont produits par nvlist_pack(NV_ENCODE_XDR).
"""

import unittest

from zfsrescue.nvlist import NvlistError, parse_packed_nvlist

from . import oracle_nvpair as O


@unittest.skipUnless(O.available, "libnvpair indisponible")
class TestNvlistAgainstLibnvpair(unittest.TestCase):

    def decode(self, builder):
        data = builder.pack_xdr()
        nvl, hdr, used = parse_packed_nvlist(data)
        self.assertEqual(hdr.encoding_name, "xdr")
        self.assertEqual(used, len(data),
                         "le decodeur doit consommer exactement le tampon packe")
        self.assertEqual(nvl.anomalies, [], "aucune desynchronisation attendue")
        return nvl.to_dict()

    def test_scalars(self):
        b = O.NvlistBuilder()
        b.add_uint64("guid", 0xDEADBEEFCAFEBABE)
        b.add_int64("neg64", -1234567890123)
        b.add_uint32("u32", 4000000000)
        b.add_int32("i32", -42)
        b.add_int16("i16", -32768)
        b.add_uint8("u8", 200)
        b.add_boolean_value("bv_true", True)
        b.add_boolean_value("bv_false", False)
        b.add_boolean("flag")
        got = self.decode(b)
        self.assertEqual(got["guid"], 0xDEADBEEFCAFEBABE)
        self.assertEqual(got["neg64"], -1234567890123)
        self.assertEqual(got["u32"], 4000000000)
        self.assertEqual(got["i32"], -42)
        self.assertEqual(got["i16"], -32768)
        self.assertEqual(got["u8"], 200)
        self.assertEqual(got["bv_true"], 1)
        self.assertEqual(got["bv_false"], 0)
        self.assertIs(got["flag"], True)
        b.free()

    def test_strings_and_padding(self):
        """Les chaines XDR sont alignees sur 4 octets : on teste toutes les restes."""
        b = O.NvlistBuilder()
        for n in range(1, 9):
            b.add_string(f"s{n}", "x" * n)
        b.add_string("vide", "")
        b.add_string("accents", "ete-a-Noel")
        got = self.decode(b)
        for n in range(1, 9):
            self.assertEqual(got[f"s{n}"], "x" * n)
        self.assertEqual(got["vide"], "")
        b.free()

    def test_arrays(self):
        b = O.NvlistBuilder()
        b.add_uint64_array("u64s", [0, 1, 2**63, 2**64 - 1])
        b.add_uint8_array("u8s", [0, 1, 254, 255])
        b.add_byte_array("bytes", bytes(range(7)))
        b.add_string_array("strs", ["a", "bb", "ccc", "dddd", "eeeee"])
        got = self.decode(b)
        self.assertEqual(got["u64s"], [0, 1, 2**63, 2**64 - 1])
        self.assertEqual(got["u8s"], [0, 1, 254, 255])
        self.assertEqual(got["bytes"], bytes(range(7)).hex())
        self.assertEqual(got["strs"], ["a", "bb", "ccc", "dddd", "eeeee"])
        b.free()

    def test_nested_like_a_vdev_tree(self):
        """Reproduit la forme d'un vdev_tree reel : nvlist imbriquee + tableau."""
        children = []
        for i in range(4):
            c = O.NvlistBuilder()
            c.add_string("type", "file")
            c.add_uint64("id", i)
            c.add_uint64("guid", 10**18 + i)
            c.add_string("path", f"/images/disk{i}.img")
            children.append(c)
        tree = O.NvlistBuilder()
        tree.add_string("type", "raidz")
        tree.add_uint64("nparity", 1)
        tree.add_uint64("ashift", 12)
        tree.add_uint64("asize", 2128609280)
        tree.add_nvlist_array("children", children)

        root = O.NvlistBuilder()
        root.add_string("name", "zrtest")
        root.add_uint64("pool_guid", 16937172268484420114)
        root.add_uint64("vdev_children", 1)
        root.add_nvlist("vdev_tree", tree)

        got = self.decode(root)
        self.assertEqual(got["pool_guid"], 16937172268484420114)
        t = got["vdev_tree"]
        self.assertEqual(t["type"], "raidz")
        self.assertEqual(t["nparity"], 1)
        self.assertEqual(len(t["children"]), 4)
        for i, c in enumerate(t["children"]):
            self.assertEqual(c["id"], i)
            self.assertEqual(c["guid"], 10**18 + i)
            self.assertEqual(c["path"], f"/images/disk{i}.img")
        for c in children:
            c.free()
        tree.free()
        root.free()


class TestNvlistRobustness(unittest.TestCase):
    def test_refuse_encodage_natif(self):
        with self.assertRaises(NvlistError):
            parse_packed_nvlist(b"\x00\x01\x00\x00" + b"\x00" * 32)

    def test_tampon_trop_court(self):
        with self.assertRaises(NvlistError):
            parse_packed_nvlist(b"\x01")

    def test_tampon_tronque(self):
        with self.assertRaises(NvlistError):
            parse_packed_nvlist(b"\x01\x01\x00\x00" + b"\x00" * 3)
