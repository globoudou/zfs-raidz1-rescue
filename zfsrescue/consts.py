"""
Constantes ZFS.

TOUTES les valeurs de ce module sont recopiees depuis la source OpenZFS 2.3.9
(paquet Debian zfs-linux 2.3.9-0+deb13u1, decompresse dans vendor/).
La reference exacte est indiquee pour chaque constante : ne jamais modifier
une valeur ici sans re-verifier le fichier source correspondant.
"""

# --- include/sys/vdev_impl.h:474-478 -----------------------------------------
VDEV_PAD_SIZE = 8 << 10                      # 8 Kio  (vl_pad1)
VDEV_SKIP_SIZE = VDEV_PAD_SIZE * 2           # 16 Kio (vl_pad1 + vl_be)
VDEV_PHYS_SIZE = 112 << 10                   # 112 Kio (vl_vdev_phys)
VDEV_UBERBLOCK_RING = 128 << 10              # 128 Kio (vl_uberblock)

# --- include/sys/vdev_impl.h:531-536 : typedef struct vdev_label -------------
#   char         vl_pad1[8K]
#   vdev_boot_envblock_t vl_be[8K]
#   vdev_phys_t  vl_vdev_phys[112K]
#   char         vl_uberblock[128K]
VDEV_LABEL_SIZE = VDEV_SKIP_SIZE + VDEV_PHYS_SIZE + VDEV_UBERBLOCK_RING   # 256 Kio
OFFSETOF_VDEV_PHYS = VDEV_SKIP_SIZE                                       # 16 Kio
OFFSETOF_UBERBLOCK_RING = VDEV_SKIP_SIZE + VDEV_PHYS_SIZE                 # 128 Kio

# --- include/sys/vdev_impl.h:546-558 -----------------------------------------
VDEV_BOOT_SIZE = 7 << 19                                       # 3,5 Mio
VDEV_LABEL_START_SIZE = 2 * VDEV_LABEL_SIZE + VDEV_BOOT_SIZE   # 4 Mio
VDEV_LABEL_END_SIZE = 2 * VDEV_LABEL_SIZE                      # 512 Kio
VDEV_LABELS = 4

# --- include/sys/uberblock_impl.h / vdev_impl.h:486-495 ----------------------
UBERBLOCK_SHIFT = 10                 # uberblock minimum : 1 Kio
MAX_UBERBLOCK_SHIFT = 13             # uberblock maximum : 8 Kio
UBERBLOCK_MAGIC = 0x00BAB10C

# --- include/sys/zio.h:53-58 : zio_eck_t -------------------------------------
#   uint64_t zec_magic ; zio_cksum_t zec_cksum (4 x uint64)
ZEC_MAGIC = 0x0210DA7AB10C7A11
ZIO_ECK_SIZE = 8 + 32                # 40 octets

# --- include/sys/nvpair.h:38-72 : data_type_t --------------------------------
DATA_TYPE_UNKNOWN = 0
DATA_TYPE_BOOLEAN = 1
DATA_TYPE_BYTE = 2
DATA_TYPE_INT16 = 3
DATA_TYPE_UINT16 = 4
DATA_TYPE_INT32 = 5
DATA_TYPE_UINT32 = 6
DATA_TYPE_INT64 = 7
DATA_TYPE_UINT64 = 8
DATA_TYPE_STRING = 9
DATA_TYPE_BYTE_ARRAY = 10
DATA_TYPE_INT16_ARRAY = 11
DATA_TYPE_UINT16_ARRAY = 12
DATA_TYPE_INT32_ARRAY = 13
DATA_TYPE_UINT32_ARRAY = 14
DATA_TYPE_INT64_ARRAY = 15
DATA_TYPE_UINT64_ARRAY = 16
DATA_TYPE_STRING_ARRAY = 17
DATA_TYPE_HRTIME = 18
DATA_TYPE_NVLIST = 19
DATA_TYPE_NVLIST_ARRAY = 20
DATA_TYPE_BOOLEAN_VALUE = 21
DATA_TYPE_INT8 = 22
DATA_TYPE_UINT8 = 23
DATA_TYPE_BOOLEAN_ARRAY = 24
DATA_TYPE_INT8_ARRAY = 25
DATA_TYPE_UINT8_ARRAY = 26
DATA_TYPE_DOUBLE = 27

DATA_TYPE_NAMES = {
    DATA_TYPE_BOOLEAN: "boolean", DATA_TYPE_BYTE: "byte",
    DATA_TYPE_INT16: "int16", DATA_TYPE_UINT16: "uint16",
    DATA_TYPE_INT32: "int32", DATA_TYPE_UINT32: "uint32",
    DATA_TYPE_INT64: "int64", DATA_TYPE_UINT64: "uint64",
    DATA_TYPE_STRING: "string", DATA_TYPE_BYTE_ARRAY: "byte[]",
    DATA_TYPE_INT16_ARRAY: "int16[]", DATA_TYPE_UINT16_ARRAY: "uint16[]",
    DATA_TYPE_INT32_ARRAY: "int32[]", DATA_TYPE_UINT32_ARRAY: "uint32[]",
    DATA_TYPE_INT64_ARRAY: "int64[]", DATA_TYPE_UINT64_ARRAY: "uint64[]",
    DATA_TYPE_STRING_ARRAY: "string[]", DATA_TYPE_HRTIME: "hrtime",
    DATA_TYPE_NVLIST: "nvlist", DATA_TYPE_NVLIST_ARRAY: "nvlist[]",
    DATA_TYPE_BOOLEAN_VALUE: "boolean_value", DATA_TYPE_INT8: "int8",
    DATA_TYPE_UINT8: "uint8", DATA_TYPE_BOOLEAN_ARRAY: "boolean[]",
    DATA_TYPE_INT8_ARRAY: "int8[]", DATA_TYPE_UINT8_ARRAY: "uint8[]",
    DATA_TYPE_DOUBLE: "double",
}

# --- module/nvpair/nvpair.c : nvs_header_t -----------------------------------
NV_ENCODE_NATIVE = 0
NV_ENCODE_XDR = 1
NV_BIG_ENDIAN = 0
NV_LITTLE_ENDIAN = 1

# --- include/sys/fs/zfs.h : pool_state_t -------------------------------------
POOL_STATE_NAMES = {
    0: "ACTIVE", 1: "EXPORTED", 2: "DESTROYED", 3: "SPARE",
    4: "L2CACHE", 5: "UNINITIALIZED", 6: "UNAVAIL", 7: "POTENTIALLY_ACTIVE",
}

# Version du source OpenZFS utilise comme reference normative
OPENZFS_REFERENCE = "OpenZFS 2.3.9 (Debian zfs-linux 2.3.9-0+deb13u1)"
