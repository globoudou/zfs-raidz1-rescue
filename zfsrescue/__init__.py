"""
zfsrescue — lecteur forensic ZFS, strictement en lecture seule.

Perimetre actuel (voir CLAUDE.md) : pool RAIDZ1 de 4 vdev dont 2 absents.
Etapes realisees : labels, mapping RAIDZ, uberblocks, MOS, navigation,
extraction, validation. Gere les vdev en partition et le balayage DSL.
"""
__version__ = "0.2.0"
