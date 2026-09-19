Objectif du projet

Je souhaite développer un outil de récupération de données forensic, strictement en lecture seule, destiné à un cas d'usage ZFS très précis.

Situation

Je dispose d'un pool ZFS constitué de :

- 4 disques physiques ;
- configuration RAIDZ1 ;
- 2 disques sont totalement détruits et irrécupérables ;
- 2 disques sont encore disponibles et doivent être considérés comme les seules sources de données fiables.

Un RAIDZ1 ne permet normalement pas de reconstruire une stripe lorsque deux disques sont absents. L'objectif n'est donc pas de reconstruire un pool ZFS fonctionnel.

L'objectif est de développer un outil capable de récupérer le maximum de données encore mathématiquement et structurellement récupérables à partir des deux disques survivants.

Le comportement recherché est proche d'un outil de récupération tel que Klennet ZFS Recovery, mais avec un périmètre beaucoup plus limité et adapté exclusivement à ce scénario.

---

Contraintes absolues

1. Ne jamais modifier les disques sources

Les deux disques survivants doivent être traités comme des supports forensic.

L'outil ne doit jamais :

- écrire sur un disque source ;
- modifier un label ZFS ;
- importer le pool en mode écriture ;
- effectuer de "zpool replace" ;
- effectuer de reconstruction ;
- effectuer de resilver ;
- créer de pool ;
- modifier les métadonnées ZFS.

Toutes les opérations doivent être effectuées en lecture seule.

Idéalement, le développement et les premiers tests doivent être réalisés sur des images bit-à-bit des disques, et jamais directement sur les disques originaux.

---

Périmètre fonctionnel initial

Le prototype doit uniquement viser :

- ZFS ;
- RAIDZ1 ;
- 4 disques ;
- 2 disques absents ;
- les 2 disques restants accessibles en lecture ;
- extraction des fichiers vers un autre support.

Ne pas chercher dans un premier temps à supporter :

- RAIDZ2 ;
- RAIDZ3 ;
- dRAID ;
- mirror ;
- configurations RAID multiples ;
- déduplication ;
- chiffrement ZFS ;
- special vdev ;
- toutes les versions historiques de ZFS ;
- toutes les architectures possibles.

Le périmètre pourra être étendu ultérieurement si le prototype fonctionne.

---

Principe général

Ne pas réimplémenter ZFS entièrement si cela peut être évité.

OpenZFS doit être utilisé comme référence technique et, lorsque la licence et l'architecture le permettent, comme source de code ou bibliothèque de référence.

L'objectif est notamment de réutiliser ou reproduire fidèlement :

- le format des labels ;
- la structure des Uberblocks ;
- la structure du MOS ;
- les dnodes ;
- les ZAP ;
- les block pointers ;
- les algorithmes RAIDZ ;
- les checksum ;
- les mécanismes de compression ;
- les structures nécessaires à la navigation dans les datasets.

Le logiciel final doit cependant être conçu comme un lecteur forensic, et non comme un gestionnaire de pool ZFS.

---

Architecture envisagée

Construire progressivement une architecture de ce type :

Disque/image 1 ─┐
                │
Disque/image 2 ─┤
                ↓
         Read-only backend
                ↓
         ZFS label parser
                ↓
         Pool/vdev discovery
                ↓
          RAIDZ mapper
                ↓
          Uberblock reader
                ↓
               MOS
                ↓
         Object sets / ZAP
                ↓
              dnodes
                ↓
          block pointers
                ↓
       RAIDZ block reconstruction
                ↓
        checksum validation
                ↓
          decompression
                ↓
         file extraction
                ↓
      recovered files + report

Les deux vdev absents doivent être représentés explicitement comme "MISSING".

---

Déroulement impératif du développement

Le développement doit être effectué par étapes courtes et validées, sans essayer de construire immédiatement l'outil complet.

Étape 0 — Analyse de l'environnement réel

Avant d'écrire du code, déterminer :

- version de ZFS utilisée par le pool ;
- système qui hébergeait le pool ;
- taille des disques ;
- taille logique et physique des secteurs ;
- configuration exacte du RAIDZ ;
- informations disponibles dans les labels des deux disques survivants.

Produire un rapport technique avant toute opération sur les données.

---

Étape 1 — Créer un environnement de test reproductible

Créer des pools ZFS de test sur plusieurs fichiers ou disques virtuels.

Exemple conceptuel :

disk0
disk1
disk2
disk3

Créer un RAIDZ1 de quatre vdevs.

Y placer des données connues :

- petits fichiers ;
- gros fichiers ;
- fichiers binaires ;
- fichiers texte ;
- arborescences ;
- fichiers suffisamment gros pour traverser plusieurs stripes ;
- fichiers compressibles et non compressibles.

Conserver les fichiers originaux comme référence.

Tester ensuite le scénario :

disk0 = MISSING
disk1 = MISSING
disk2 = disponible
disk3 = disponible

Le but est de disposer d'un environnement où le résultat attendu est connu exactement.

---

Étape 2 — Parser les labels ZFS

Développer un premier outil capable de lire uniquement les labels des deux disques survivants.

Il doit identifier :

- pool GUID ;
- vdev GUID ;
- topologie ;
- ashift ;
- taille des secteurs ;
- TXG ;
- informations pertinentes des labels ;
- emplacement des labels ;
- éventuelles incohérences.

Sortie lisible par l'humain et éventuellement JSON.

À cette étape, aucune récupération de fichier n'est nécessaire.

---

Étape 3 — Implémenter le mapping RAIDZ

C'est une étape critique.

Implémenter ou réutiliser le mécanisme de mapping RAIDZ permettant de déterminer :

logical RAIDZ block
      ↓
stripe
      ↓
disk/vdev
      ↓
offset physique

Le résultat doit être comparé au comportement d'OpenZFS.

Créer de nombreux tests unitaires avec différentes tailles de blocs, ashift et tailles de données.

Cette étape doit être entièrement validée avant de continuer.

---

Étape 4 — Lecture des Uberblocks

Implémenter la découverte des Uberblocks disponibles sur les deux disques survivants.

Identifier :

- TXG ;
- timestamps ;
- checksum ;
- références vers le MOS ;
- Uberblocks valides/invalides.

Ne pas modifier les données.

L'outil doit pouvoir déterminer quel état historique du pool est potentiellement exploitable.

---

Étape 5 — Lecture du MOS

À partir des Uberblocks valides :

- localiser le MOS ;
- lire ses objets ;
- valider les checksums ;
- gérer les blocs dont certaines parties sont absentes ;
- identifier les structures encore exploitables.

L'objectif est d'arriver à parcourir suffisamment du MOS pour retrouver les datasets.

---

Étape 6 — Navigation ZFS

Implémenter progressivement :

MOS
 ↓
DSL
 ↓
Object Set
 ↓
ZAP
 ↓
dnode
 ↓
file object

Le programme doit pouvoir afficher une arborescence de fichiers récupérables.

Exemple :

dataset/
  photos/
    2024/
      photo001.jpg
      photo002.jpg
      video001.mp4

À ce stade, aucune écriture sur les sources.

---

Étape 7 — Récupération des blocs de fichiers

Pour chaque fichier :

1. lire le dnode ;
2. récupérer les block pointers ;
3. déterminer les blocs RAIDZ nécessaires ;
4. lire les données disponibles sur les deux disques ;
5. déterminer si le bloc peut être reconstruit ;
6. vérifier le checksum ;
7. décompresser si nécessaire ;
8. écrire le résultat sur un support de destination.

Chaque bloc doit avoir un état explicite :

RECOVERED
RECOVERED_WITH_RECONSTRUCTION
INVALID_CHECKSUM
MISSING_DATA
UNKNOWN

Ne jamais considérer un bloc comme valide simplement parce qu'il produit des données plausibles.

---

Étape 8 — Gestion des fichiers partiellement récupérables

Le cas le plus important est celui où un fichier contient certains blocs récupérables et d'autres non.

Ne pas abandonner automatiquement le fichier.

Produire plutôt :

fichier.iso
  block 0       OK
  block 1       OK
  block 2       MISSING
  block 3       OK
  block 4       OK

Selon le type de fichier, permettre éventuellement une extraction partielle.

Le rapport doit indiquer clairement les fichiers :

- complètement récupérés ;
- partiellement récupérés ;
- impossibles à récupérer.

---

Étape 9 — Validation

Comparer systématiquement le résultat du programme avec les données originales des pools de test.

Mesurer :

- fichiers retrouvés ;
- taille ;
- checksum ;
- contenu ;
- blocs récupérés ;
- blocs irrécupérables.

Tester plusieurs scénarios de suppression de disques.

Ne passer à l'étape suivante que lorsque les tests précédents sont fiables.

---

Étape 10 — Application aux vraies données

Seulement lorsque le prototype est suffisamment validé :

1. faire des images bit-à-bit des deux disques survivants ;
2. conserver les originaux hors ligne ;
3. travailler exclusivement sur les images ;
4. lancer l'analyse ;
5. générer d'abord un rapport sans extraction ;
6. vérifier les résultats ;
7. effectuer ensuite l'extraction vers un troisième support.

---

Choix technologique

Privilégier :

- Linux ;
- C ou C++ pour les parties proches d'OpenZFS si nécessaire ;
- éventuellement Python pour l'orchestration, l'analyse et les outils de diagnostic ;
- tests automatisés ;
- CLI dans un premier temps.

Ne pas commencer par une interface graphique.

La priorité est :

exactitude
sécurité
lecture seule
reproductibilité
tests

et non l'ergonomie.

---

Utilisation de l'IA

Utiliser l'agent IA comme assistant de développement, mais ne jamais lui faire confiance aveuglément sur les structures binaires ZFS.

Pour chaque composant critique :

1. rechercher la définition officielle dans le code OpenZFS ;
2. identifier la version de ZFS concernée ;
3. implémenter ;
4. créer un test reproductible ;
5. comparer avec OpenZFS ;
6. documenter les différences éventuelles.

L'agent doit privilégier la lecture du code source OpenZFS aux suppositions ou aux descriptions trouvées sur Internet.

Pour les structures binaires, toujours vérifier :

- endianess ;
- tailles exactes ;
- alignement ;
- offsets ;
- checksum ;
- version ;
- évolution entre versions ZFS.

---

Règle fondamentale

Ne jamais essayer de "deviner" une donnée absente.

Si deux disques sont nécessaires pour reconstruire une donnée et qu'ils sont absents, le logiciel doit déclarer cette donnée comme irrécupérable.

En revanche, il doit chercher toutes les informations indépendantes encore disponibles dans :

- les deux disques survivants ;
- les métadonnées ZFS ;
- les différents Uberblocks ;
- les snapshots ;
- les block pointers ;
- les copies éventuelles de métadonnées.

L'objectif est de récupérer tout ce qui est réellement récupérable, pas de fabriquer une reconstruction artificielle.

---

Première livraison attendue

Ne pas commencer par développer l'ensemble du système.

La première version doit simplement :

1. ouvrir deux images de disques en lecture seule ;
2. détecter les labels ZFS ;
3. identifier le pool ;
4. identifier la topologie RAIDZ1 ;
5. confirmer la présence de 4 vdevs ;
6. représenter deux vdevs comme "MISSING" ;
7. afficher les paramètres nécessaires au mapping RAIDZ ;
8. produire un rapport JSON ;
9. ne modifier absolument aucune donnée.

Une fois cette étape validée, nous continuerons avec les Uberblocks puis le MOS.

À chaque étape, fournir :

- le code ;
- les tests ;
- les commandes permettant de reproduire les tests ;
- les résultats attendus ;
- les limitations connues ;
- une explication des structures ZFS utilisées.

Ne jamais sauter plusieurs étapes simultanément.
