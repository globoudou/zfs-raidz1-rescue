"""
Couche ZPL : repertoires et fichiers d'un dataset.

Reference normative : OpenZFS 2.3.9
  * noeud maitre           include/sys/zfs_znode.h:137-142 (ROOT, SA_ATTRS...)
  * entrees de repertoire  include/sys/zfs_znode.h:158-159
        valeur = type sur 4 bits (bits 60-63) + numero d'objet (bits 0-47)
  * System Attributes      include/sys/sa_impl.h:69-92, 162-186
        REGISTRY : nom -> ATTR_NUM(0-15) ATTR_BSWAP(16-23) ATTR_LENGTH(24-39)
        LAYOUTS  : numero -> tableau d'identifiants d'attributs (uint16)
        en-tete du bonus : magic(4) + layout_info(2) + longueurs variables
  * table ZPL de secours   module/zfs/zfs_sa.c:48-72 (si pas de registre)

Un fichier moderne range ses metadonnees (taille, mode, dates...) dans les
System Attributes du bonus du dnode. Rien n'est devine : la disposition est
lue dans le registre du dataset.
"""

from __future__ import annotations

import datetime as _dt
import stat as _stat
import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

from .dmu import Dnode, DmuReader, Objset
from .zap import Zap, read_zap

SA_MAGIC = 0x2F505A

# module/zfs/zfs_sa.c:48 — table utilisee quand le dataset n'a pas de registre
ZPL_LEGACY_ATTRS = [
    ("ZPL_ATIME", 16), ("ZPL_MTIME", 16), ("ZPL_CTIME", 16),
    ("ZPL_CRTIME", 16), ("ZPL_GEN", 8), ("ZPL_MODE", 8), ("ZPL_SIZE", 8),
    ("ZPL_PARENT", 8), ("ZPL_LINKS", 8), ("ZPL_XATTR", 8), ("ZPL_RDEV", 8),
    ("ZPL_FLAGS", 8), ("ZPL_UID", 8), ("ZPL_GID", 8), ("ZPL_PAD", 32),
    ("ZPL_ZNODE_ACL", 88),
]

# module/zfs/zfs_sa.c:48-72 — numerotation standard des attributs ZPL.
# Le REGISTRY d'un dataset attribue ces numeros dans cet ordre ; nous nous en
# servons UNIQUEMENT en dernier recours, et toujours avec validation.
ZPL_STANDARD_ATTRS = [
    ("ZPL_ATIME", 16), ("ZPL_MTIME", 16), ("ZPL_CTIME", 16),
    ("ZPL_CRTIME", 16), ("ZPL_GEN", 8), ("ZPL_MODE", 8), ("ZPL_SIZE", 8),
    ("ZPL_PARENT", 8), ("ZPL_LINKS", 8), ("ZPL_XATTR", 8), ("ZPL_RDEV", 8),
    ("ZPL_FLAGS", 8), ("ZPL_UID", 8), ("ZPL_GID", 8), ("ZPL_PAD", 32),
    ("ZPL_ZNODE_ACL", 88), ("ZPL_DACL_COUNT", 8), ("ZPL_SYMLINK", 0),
    ("ZPL_SCANSTAMP", 32), ("ZPL_DACL_ACES", 0), ("ZPL_DXATTR", 0),
    ("ZPL_PROJID", 8),
]
A = {nom: i for i, (nom, _l) in enumerate(ZPL_STANDARD_ATTRS)}

# Dispositions candidates, telles que les compose zfs_mknode() (zfs_znode.c).
# Elles ne servent qu'a formuler une HYPOTHESE, qui doit ensuite passer les
# controles de parse_sa_bonus + valider_attributs.
DISPOSITIONS_CANDIDATES = [
    [A["ZPL_MODE"], A["ZPL_SIZE"], A["ZPL_GEN"], A["ZPL_UID"], A["ZPL_GID"],
     A["ZPL_PARENT"], A["ZPL_FLAGS"], A["ZPL_ATIME"], A["ZPL_MTIME"],
     A["ZPL_CTIME"], A["ZPL_CRTIME"], A["ZPL_LINKS"], A["ZPL_PROJID"],
     A["ZPL_DACL_COUNT"], A["ZPL_DACL_ACES"]],
    [A["ZPL_MODE"], A["ZPL_SIZE"], A["ZPL_GEN"], A["ZPL_UID"], A["ZPL_GID"],
     A["ZPL_PARENT"], A["ZPL_FLAGS"], A["ZPL_ATIME"], A["ZPL_MTIME"],
     A["ZPL_CTIME"], A["ZPL_CRTIME"], A["ZPL_LINKS"],
     A["ZPL_DACL_COUNT"], A["ZPL_DACL_ACES"]],
    [A["ZPL_MODE"], A["ZPL_SIZE"], A["ZPL_GEN"], A["ZPL_UID"], A["ZPL_GID"],
     A["ZPL_PARENT"], A["ZPL_FLAGS"], A["ZPL_ATIME"], A["ZPL_MTIME"],
     A["ZPL_CTIME"], A["ZPL_CRTIME"], A["ZPL_LINKS"], A["ZPL_PROJID"],
     A["ZPL_DACL_COUNT"], A["ZPL_SYMLINK"], A["ZPL_DACL_ACES"]],
    [A["ZPL_MODE"], A["ZPL_SIZE"], A["ZPL_GEN"], A["ZPL_UID"], A["ZPL_GID"],
     A["ZPL_PARENT"], A["ZPL_FLAGS"], A["ZPL_ATIME"], A["ZPL_MTIME"],
     A["ZPL_CTIME"], A["ZPL_CRTIME"], A["ZPL_LINKS"], A["ZPL_RDEV"],
     A["ZPL_PROJID"], A["ZPL_DACL_COUNT"], A["ZPL_DACL_ACES"]],
]

# Types d'entree de repertoire : ce sont les bits de mode decales de 12
DIRENT_TYPES = {1: "fifo", 2: "chardev", 4: "dir", 6: "blockdev",
                8: "file", 10: "symlink", 12: "socket", 14: "whiteout"}


class ZplError(Exception):
    pass


@dataclass
class SaAttributeTable:
    """Registre et dispositions des System Attributes d'un dataset."""
    registry: dict[int, tuple[str, int]] = field(default_factory=dict)  # num -> (nom, longueur)
    layouts: dict[int, list[int]] = field(default_factory=dict)          # num -> [attr...]
    legacy: bool = False
    errors: list[str] = field(default_factory=list)

    def name_of(self, attr: int) -> str:
        return self.registry.get(attr, (f"attr_{attr}", 0))[0]

    def length_of(self, attr: int) -> int:
        return self.registry.get(attr, ("", 0))[1]


def read_sa_table(dmu: DmuReader, objset: Objset, sa_attrs_obj: int
                  ) -> SaAttributeTable:
    t = SaAttributeTable()
    dn, _ = dmu.read_dnode(objset.meta_dnode, sa_attrs_obj)
    if dn is None or not dn.allocated:
        t.errors.append(f"objet SA_ATTRS {sa_attrs_obj} illisible")
        return t
    racine = read_zap(dmu, dn)
    if not racine.complete:
        t.errors.append("ZAP SA_ATTRS incomplet")

    obj_reg = racine.get("REGISTRY")
    obj_lay = racine.get("LAYOUTS")

    if obj_reg:
        dnr, _ = dmu.read_dnode(objset.meta_dnode, int(obj_reg))
        if dnr is None:
            t.errors.append("registre SA illisible")
        else:
            z = read_zap(dmu, dnr)
            if not z.complete:
                t.errors.append("registre SA incomplet")
            for e in z.entries:
                if not e.values:
                    continue
                v = e.values[0]
                num = v & 0xFFFF                 # ATTR_NUM
                longueur = (v >> 24) & 0xFFFF    # ATTR_LENGTH (0 = variable)
                t.registry[num] = (e.name, longueur)
    if obj_lay:
        dnl, _ = dmu.read_dnode(objset.meta_dnode, int(obj_lay))
        if dnl is None:
            t.errors.append("dispositions SA illisibles")
        else:
            z = read_zap(dmu, dnl)
            if not z.complete:
                t.errors.append("dispositions SA incompletes")
            for e in z.entries:
                try:
                    t.layouts[int(e.name)] = list(e.values)
                except ValueError:
                    t.errors.append(f"numero de disposition invalide : {e.name}")
    if not t.registry:
        t.legacy = True
        for i, (nom, longueur) in enumerate(ZPL_LEGACY_ATTRS):
            t.registry[i] = (nom, longueur)
    return t


def valider_attributs(attrs: dict[str, Any], dn: Dnode | None,
                      consomme: int, bonuslen: int) -> list[str]:
    """
    Controles de vraisemblance d'un decodage SA. Sert a accepter ou rejeter une
    hypothese de disposition : aucune hypothese n'est retenue sans ces controles.
    """
    pb = []
    if consomme != bonuslen:
        pb.append(f"les attributs occupent {consomme} octets, le bonus en fait "
                  f"{bonuslen}")
    mode = attrs.get("ZPL_MODE")
    if not isinstance(mode, int) or mode == 0:
        pb.append("ZPL_MODE absent ou nul")
    else:
        fmt = mode & 0o170000
        if fmt not in (0o100000, 0o40000, 0o120000, 0o10000, 0o20000,
                       0o60000, 0o140000):
            pb.append(f"ZPL_MODE {oct(mode)} : type de fichier invalide")
        if dn is not None:
            attendu_rep = dn.type == DMU_OT_DIRECTORY_CONTENTS
            est_rep = fmt == 0o40000
            if attendu_rep != est_rep:
                pb.append("ZPL_MODE incoherent avec le type du dnode")
    liens = attrs.get("ZPL_LINKS")
    if not isinstance(liens, int) or liens == 0 or liens > 1 << 20:
        pb.append("ZPL_LINKS invalide")
    taille = attrs.get("ZPL_SIZE")
    if not isinstance(taille, int):
        pb.append("ZPL_SIZE absent")
    elif dn is not None and dn.allocated:
        maxi = (dn.maxblkid + 1) * dn.datablksize
        if taille > maxi:
            pb.append(f"ZPL_SIZE {taille} > espace alloue {maxi}")
    for cle in ("ZPL_MTIME", "ZPL_CRTIME"):
        v = attrs.get(cle)
        if isinstance(v, tuple) and not (0 < v[0] < 4102444800):   # < an 2100
            pb.append(f"{cle} hors plage plausible")
    return pb


def parse_sa_bonus(bonus: bytes, table: SaAttributeTable
                   ) -> tuple[dict[str, Any], list[str]]:
    """
    Decode le bonus d'un dnode au format System Attributes.
    Retourne (attributs, erreurs). Les valeurs de 8 octets sont rendues en
    entier, les autres en octets bruts.
    """
    erreurs: list[str] = []
    if len(bonus) < 8:
        return {}, ["bonus trop court pour un en-tete SA"]
    magic, layout_info = struct.unpack_from("<IH", bonus, 0)
    if magic != SA_MAGIC:
        return {}, [f"magic SA invalide : {magic:#x}"]

    layout_num = layout_info & 0x3FF          # SA_HDR_LAYOUT_NUM
    hdrsz = ((layout_info >> 10) & 0x3F) << 3  # SA_HDR_SIZE
    attrs_ids = table.layouts.get(layout_num)
    if attrs_ids is None:
        return {}, [f"disposition SA {layout_num} inconnue"]

    # longueurs des attributs de taille variable, dans l'ordre de la disposition
    variables = [a for a in attrs_ids if table.length_of(a) == 0]
    longueurs: list[int] = []
    for i in range(len(variables)):
        off = 6 + i * 2
        if off + 2 > hdrsz:
            erreurs.append("en-tete SA trop court pour les longueurs variables")
            break
        longueurs.append(struct.unpack_from("<H", bonus, off)[0])

    out: dict[str, Any] = {}
    pos = hdrsz
    iv = 0
    for a in attrs_ids:
        nom = table.name_of(a)
        longueur = table.length_of(a)
        if longueur == 0:
            if iv >= len(longueurs):
                erreurs.append(f"longueur manquante pour {nom}")
                break
            longueur = longueurs[iv]
            iv += 1
        if pos + longueur > len(bonus):
            erreurs.append(f"attribut {nom} hors du bonus")
            break
        brut = bonus[pos:pos + longueur]
        pos += longueur
        if longueur == 8:
            out[nom] = struct.unpack("<Q", brut)[0]
        elif longueur == 16:
            out[nom] = struct.unpack("<2Q", brut)      # secondes, nanosecondes
        else:
            out[nom] = brut
    out["__consomme__"] = pos
    return out, erreurs


def inferer_disposition_sa(bonus: bytes, dn: Dnode | None
                           ) -> tuple[dict[str, Any], list[int] | None, list[str]]:
    """
    Dernier recours : la disposition SA du dataset est illisible.

    On essaie les dispositions standard connues et on ne retient un resultat
    que si UNE SEULE passe tous les controles de vraisemblance. Le resultat est
    marque comme issu d'une hypothese : il ne doit jamais etre presente comme
    une lecture directe.
    """
    table = SaAttributeTable()
    for i, (nom, longueur) in enumerate(ZPL_STANDARD_ATTRS):
        table.registry[i] = (nom, longueur)

    if len(bonus) < 8:
        return {}, None, ["bonus trop court"]
    magic, layout_info = struct.unpack_from("<IH", bonus, 0)
    if magic != SA_MAGIC:
        return {}, None, [f"magic SA invalide : {magic:#x}"]
    num = layout_info & 0x3FF

    retenues = []
    for candidate in DISPOSITIONS_CANDIDATES:
        table.layouts = {num: candidate}
        attrs, erreurs = parse_sa_bonus(bonus, table)
        if erreurs:
            continue
        consomme = attrs.pop("__consomme__", -1)
        pb = valider_attributs(attrs, dn, consomme, len(bonus))
        if not pb:
            retenues.append((candidate, attrs))

    if len(retenues) == 1:
        candidate, attrs = retenues[0]
        return attrs, candidate, []
    if not retenues:
        return {}, None, [f"disposition SA {num} inconnue et aucune hypothese "
                          "standard ne passe les controles"]
    return {}, None, [f"disposition SA {num} inconnue et {len(retenues)} "
                      "hypotheses concurrentes : refus de choisir"]


@dataclass
class ZplEntry:
    """Une entree de repertoire."""
    name: str
    object_id: int
    type_code: int

    @property
    def type_name(self) -> str:
        return DIRENT_TYPES.get(self.type_code, f"type_{self.type_code}")

    @property
    def is_dir(self) -> bool:
        return self.type_code == 4


@dataclass
class ZplNode:
    """Un fichier ou repertoire, avec ses metadonnees SA."""
    object_id: int
    name: str
    path: str
    dnode: Dnode | None
    attrs: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    errors_attrs: list[str] = field(default_factory=list)
    entry_type: str = ""

    @property
    def size(self) -> int | None:
        v = self.attrs.get("ZPL_SIZE")
        return v if isinstance(v, int) else None

    @property
    def mode(self) -> int | None:
        v = self.attrs.get("ZPL_MODE")
        return v if isinstance(v, int) else None

    @property
    def links(self) -> int | None:
        return self.attrs.get("ZPL_LINKS")

    @property
    def mtime(self) -> tuple | None:
        v = self.attrs.get("ZPL_MTIME")
        return v if isinstance(v, tuple) else None

    @property
    def mtime_iso(self) -> str:
        m = self.mtime
        if not m:
            return ""
        return _dt.datetime.fromtimestamp(m[0], _dt.timezone.utc).isoformat()

    @property
    def is_dir(self) -> bool:
        m = self.mode
        return bool(m and _stat.S_ISDIR(m))

    @property
    def is_file(self) -> bool:
        m = self.mode
        return bool(m and _stat.S_ISREG(m))

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id, "path": self.path, "name": self.name,
            "type": self.entry_type,
            "size": self.size, "mode": oct(self.mode) if self.mode else None,
            "links": self.links, "mtime_utc": self.mtime_iso,
            "uid": self.attrs.get("ZPL_UID"), "gid": self.attrs.get("ZPL_GID"),
            "dnode": self.dnode.as_dict() if self.dnode else None,
            "errors": self.errors,
        }


class ZplReader:
    """Parcours des fichiers d'un dataset ZFS."""

    def __init__(self, dmu: DmuReader, objset: Objset,
                 allow_inference: bool = True):
        self.dmu = dmu
        self.objset = objset
        self.master: Zap | None = None
        self.sa_table: SaAttributeTable | None = None
        self.errors: list[str] = []
        self.allow_inference = allow_inference
        self.inferred_layouts = 0
        self._prepare()

    @staticmethod
    def _table_legacy() -> SaAttributeTable:
        t = SaAttributeTable(legacy=True)
        for i, (nom, longueur) in enumerate(ZPL_LEGACY_ATTRS):
            t.registry[i] = (nom, longueur)
        return t

    def _prepare(self) -> None:
        self.sa_table = self._table_legacy()
        dn, _ = self.dmu.read_dnode(self.objset.meta_dnode, 1)
        if dn is None or not dn.allocated:
            self.errors.append("noeud maitre (objet 1) illisible")
            return
        self.master = read_zap(self.dmu, dn)
        if not self.master.complete:
            self.errors.append("noeud maitre incomplet")
        sa_obj = self.master.get("SA_ATTRS")
        if sa_obj:
            self.sa_table = read_sa_table(self.dmu, self.objset, int(sa_obj))
            self.errors += self.sa_table.errors
        else:
            self.errors.append("SA_ATTRS absent : table ZPL historique utilisee")

    @property
    def root_object(self) -> int | None:
        if self.master is None:
            return None
        v = self.master.get("ROOT")
        return int(v) if v else None

    @property
    def version(self) -> int | None:
        if self.master is None:
            return None
        v = self.master.get("VERSION")
        return int(v) if v else None

    def node(self, object_id: int, name: str = "", path: str = "",
             entry_type: str = "") -> ZplNode:
        dn, etat = self.dmu.read_dnode(self.objset.meta_dnode, object_id)
        n = ZplNode(object_id=object_id, name=name, path=path, dnode=dn,
                    entry_type=entry_type)
        if dn is None:
            n.errors.append(f"dnode illisible ({etat})")
            return n
        if not dn.allocated:
            n.errors.append("dnode non alloue")
            return n
        if dn.bonus and self.sa_table is not None:
            n.attrs, n.errors_attrs = self.decode_bonus(dn)
            n.errors += n.errors_attrs
        elif not dn.bonus:
            n.errors.append("bonus absent : metadonnees inconnues")
        return n

    def decode_bonus(self, dn: Dnode) -> tuple[dict[str, Any], list[str]]:
        """
        Decode le bonus SA d'un dnode. Si la disposition du dataset est
        illisible, tente une hypothese validee (marquee comme telle).
        """
        attrs, erreurs = parse_sa_bonus(dn.bonus, self.sa_table)
        attrs.pop("__consomme__", None)
        if attrs and not erreurs:
            return attrs, []
        if not self.allow_inference:
            return {}, erreurs
        attrs, disposition, erreurs2 = inferer_disposition_sa(dn.bonus, dn)
        if attrs:
            attrs["__hypothese__"] = True
            self.inferred_layouts += 1
            return attrs, ["attributs reconstitues par hypothese validee "
                           "(disposition SA du dataset illisible)"]
        return {}, erreurs + erreurs2

    def read_directory(self, object_id: int) -> tuple[list[ZplEntry], Zap]:
        dn, etat = self.dmu.read_dnode(self.objset.meta_dnode, object_id)
        if dn is None or not dn.allocated:
            raise ZplError(f"repertoire {object_id} illisible ({etat})")
        z = read_zap(self.dmu, dn)
        entrees = []
        for e in z.entries:
            if not e.values:
                continue
            v = e.values[0]
            entrees.append(ZplEntry(name=e.name,
                                    object_id=v & ((1 << 48) - 1),
                                    type_code=(v >> 60) & 0xF))
        entrees.sort(key=lambda x: x.name)
        return entrees, z

    def walk(self, object_id: int | None = None, path: str = "",
             profondeur: int = 0, max_depth: int = 64
             ) -> Iterator[tuple[ZplNode, list[str]]]:
        """
        Parcourt recursivement l'arborescence.
        Rend (noeud, avertissements) pour chaque fichier et repertoire.
        """
        if object_id is None:
            object_id = self.root_object
            if object_id is None:
                return
        if profondeur > max_depth:
            return

        try:
            entrees, z = self.read_directory(object_id)
        except ZplError as exc:
            yield (self.node(object_id, path=path or "/", entry_type="dir"),
                   [str(exc)])
            return

        avertissements = []
        if not z.complete:
            avertissements.append(
                f"repertoire {path or '/'} incomplet : "
                f"{len(z.entries)} entrees lues, {z.declared_entries} annoncees, "
                f"blocs manquants {z.blocks_missing}")
        if avertissements:
            yield (self.node(object_id, path=path or "/", entry_type="dir"),
                   avertissements)

        for e in entrees:
            chemin = f"{path}/{e.name}"
            n = self.node(e.object_id, name=e.name, path=chemin,
                          entry_type=e.type_name)
            yield n, []
            if e.is_dir:
                yield from self.walk(e.object_id, chemin, profondeur + 1,
                                     max_depth)


# --- reconstruction par balayage des dnodes ----------------------------------
DMU_OT_PLAIN_FILE_CONTENTS = 19
DMU_OT_DIRECTORY_CONTENTS = 20


@dataclass
class IndexedNode:
    """Un objet du dataset, retrouve par le repertoire ou par balayage."""
    object_id: int
    dnode: Dnode | None
    name: str = ""
    parent_obj: int | None = None
    path: str = ""
    source: str = "balayage"        # "repertoire" ou "balayage"
    attrs: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def is_dir(self) -> bool:
        return bool(self.dnode and self.dnode.type == DMU_OT_DIRECTORY_CONTENTS)

    @property
    def is_file(self) -> bool:
        return bool(self.dnode and self.dnode.type == DMU_OT_PLAIN_FILE_CONTENTS)

    @property
    def size(self) -> int | None:
        v = self.attrs.get("ZPL_SIZE")
        return v if isinstance(v, int) else None

    @property
    def attrs_inferred(self) -> bool:
        return bool(self.attrs.get("__hypothese__"))

    @property
    def mode(self) -> int | None:
        v = self.attrs.get("ZPL_MODE")
        return v if isinstance(v, int) else None

    @property
    def mtime_iso(self) -> str:
        m = self.attrs.get("ZPL_MTIME")
        if not isinstance(m, tuple):
            return ""
        return _dt.datetime.fromtimestamp(m[0], _dt.timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id, "path": self.path, "name": self.name,
            "kind": "dir" if self.is_dir else ("file" if self.is_file else
                                               (self.dnode.type_name
                                                if self.dnode else "?")),
            "size": self.size, "mode": oct(self.mode) if self.mode else None,
            "mtime_utc": self.mtime_iso, "parent_obj": self.parent_obj,
            "source": self.source, "attrs_inferred": self.attrs_inferred,
            "errors": self.errors,
            "blocks": self.dnode.nblocks if self.dnode else 0,
            "blocksize": self.dnode.datablksize if self.dnode else 0,
        }


@dataclass
class DatasetIndex:
    """Index complet d'un dataset : ce qui a pu etre retrouve, et comment."""
    nodes: dict[int, IndexedNode] = field(default_factory=dict)
    root_object: int | None = None
    dnodes_total: int = 0
    dnodes_unreadable: int = 0
    directories_read: int = 0
    directories_incomplete: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[IndexedNode]:
        return [n for n in self.nodes.values() if n.is_file]

    @property
    def named(self) -> list[IndexedNode]:
        return [n for n in self.nodes.values() if n.source == "repertoire"]

    @property
    def orphans(self) -> list[IndexedNode]:
        return [n for n in self.nodes.values()
                if n.source == "balayage" and (n.is_file or n.is_dir)]

    def summary(self) -> dict[str, Any]:
        return {
            "root_object": self.root_object,
            "dnodes_total": self.dnodes_total,
            "dnodes_unreadable": self.dnodes_unreadable,
            "objects_found": len(self.nodes),
            "files": len(self.files),
            "named_from_directories": len(self.named),
            "orphans": len(self.orphans),
            "directories_read": self.directories_read,
            "directories_incomplete": self.directories_incomplete,
            "errors": self.errors,
        }


def build_index(zpl: "ZplReader") -> DatasetIndex:
    """
    Recense TOUS les objets encore lisibles du dataset :

      1. balayage complet du tableau des dnodes (independant des repertoires) ;
      2. lecture de chaque repertoire lisible pour retrouver les noms ;
      3. reconstruction des chemins tant que la chaine de parents est lisible.

    Les objets dont le repertoire parent est perdu restent presents, sous un
    chemin explicitement marque comme non reconstruit.
    """
    idx = DatasetIndex(root_object=zpl.root_object)
    idx.errors += zpl.errors

    # 1. balayage
    for oid, dn, etat in zpl.dmu.iter_dnodes(zpl.objset.meta_dnode):
        idx.dnodes_total += 1
        if dn is None:
            idx.dnodes_unreadable += 1
            continue
        if not dn.allocated:
            continue
        n = IndexedNode(object_id=oid, dnode=dn)
        if dn.bonus and zpl.sa_table is not None:
            n.attrs, erreurs = zpl.decode_bonus(dn)
            n.errors += erreurs
        p = n.attrs.get("ZPL_PARENT")
        if isinstance(p, int):
            n.parent_obj = p
        idx.nodes[oid] = n

    # 2. noms depuis les repertoires lisibles
    for oid, n in list(idx.nodes.items()):
        if not n.is_dir:
            continue
        try:
            entrees, z = zpl.read_directory(oid)
        except ZplError as exc:
            n.errors.append(str(exc))
            continue
        idx.directories_read += 1
        if not z.complete:
            idx.directories_incomplete.append(oid)
        for e in entrees:
            cible = idx.nodes.get(e.object_id)
            if cible is None:
                cible = IndexedNode(object_id=e.object_id, dnode=None,
                                    errors=["dnode absent du tableau"])
                idx.nodes[e.object_id] = cible
            cible.name = e.name
            cible.parent_obj = oid
            cible.source = "repertoire"

    # 3. chemins
    def est_racine(oid: int) -> bool:
        """La racine d'un dataset ZFS est son propre parent (zfs_znode.c)."""
        if oid == idx.root_object:
            return True
        n = idx.nodes.get(oid)
        return bool(n and n.is_dir and n.parent_obj == oid)

    def chemin(oid: int, vus: set[int]) -> str:
        n = idx.nodes.get(oid)
        if n is None:
            return f"/(objet {oid} absent)"
        if est_racine(oid):
            return ""
        if oid in vus:
            return f"/(boucle {oid})"
        vus.add(oid)
        if n.parent_obj is None or n.parent_obj not in idx.nodes:
            base = f"/(parent perdu)/objet_{oid}" if not n.name \
                else f"/(parent perdu)/{n.name}"
            return base
        return f"{chemin(n.parent_obj, vus)}/{n.name or f'objet_{oid}'}"

    for oid, n in idx.nodes.items():
        if est_racine(oid):
            n.path = "/"
            n.name = ""
            if idx.root_object is None:
                idx.root_object = oid
                idx.errors.append(
                    f"racine deduite de l'objet {oid} (son propre parent) : "
                    "le noeud maitre du dataset est illisible")
        else:
            n.path = chemin(oid, set())
    return idx
