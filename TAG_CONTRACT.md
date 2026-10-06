# Contrat de tags — SimplyCompress (Linux) ↔ SimplyPlay (Android)

Ce document fixe le contenu de tags que les **deux** chaînes doivent produire pour une
même source audio, de sorte qu'un fichier compressé sur Linux et le même compressé sur
Android soient comparables champ par champ.

Il s'appuie sur des mesures, pas sur des hypothèses : chaque règle cite dans §12 les
observations qui l'ont motivée.

---

## 1. Contrainte non négociable : la compression ne doit pas coûter de ressources

Consigne de développement : compresser les fichiers **sans consommer beaucoup de
ressources**. Elle est déjà écrite dans le code des deux côtés et ce contrat la reprend
telle quelle :

- Linux : `simplyconvert.py:424` — `os.nice(10)  # low priority, like the app's
  MIN_PRIORITY engine` ;
- Android : `CompressionFlow.kt:358` — « OS-level low priority so playback and the UI
  always win the CPU », et `README.md` — exécution « at low priority » en arrière-plan,
  pause automatique entre deux fichiers dès que le battery saver se déclenche (« zero CPU »).

Elle prime sur toute règle de parité. En conséquence, sont **interdits** :

- tout **passe de décodage audio supplémentaire** (analyser le ReplayGain « pour avoir le
  tag avant d'écrire » coûterait un décodage complet par morceau → écarté) ;
- tout **ré-encodage**, audio comme image (ré-encoder la couverture pour « la normaliser »
  → écarté, et c'est exactement le défaut actuel de Linux, §12) ;
- toute **réécriture supplémentaire du fichier** (une deuxième passe pour rajouter des
  tags → écarté) ;
- tout **accès réseau** dans le chemin de compression (l'identification MusicBrainz reste
  un module séparé) ;
- lecture des tags au-delà du **bloc d'en-tête** : lire des commentaires ne décode jamais
  l'audio, c'est gratuit ; décoder l'audio pour en tirer une information ne l'est pas.

Toute règle de ce contrat qui serait incompatible avec §1 est à rejeter, même si elle
améliore la parité.

---

## 2. Principes

1. **Une seule orthographe par information.** Chaque concept a un nom de clé unique.
2. **Copie verbatim des valeurs.** Aucune transformation, découpe, déduplication ou
   reformatage.
3. **Rien n'est écrit s'il n'est pas dans la liste fermée** de §4.
4. **Les deux lectures doivent voir la même chose** : si un lecteur écrase une information
   que l'autre voit, aucune règle d'écriture ne rattrapera l'écart (§3).
5. **Un écart doit être détecté par le test**, pas par l'humain (§10).

---

## 3. Règle de lecture (identique des deux côtés)

**3.1 Normalisation.** Pour comparer ou rechercher une clé : `clé.trim().toLowerCase()`.
Les noms restent sinon tels quels : `album_artist` et `albumartist` sont **deux noms
distincts**, pas deux graphies d'un même nom.

**3.2 Alias acceptés en lecture** (une seule valeur est retenue pour la clé canonique) :

| canonique | alias lus en entrée |
|---|---|
| `albumartist` | `album_artist`, `album artist` |
| `tracknumber` | `track`, `trck` |
| `discnumber` | `disc`, `tpos` |
| `label` | `publisher`, `tpub` |
| `date` | `year`, `originaldate`, `tyer`, `tdrc` |
| `genre` | `tcon` |
| `composer` | `tcom` |
| `comment` | `description`, `comm` |
| `bpm` | `tbpm` |
| `copyright` | `tcop` |
| `catalognumber` | `catalog number` |
| `lyrics` | `unsy` |
| `album` | `talb` |
| `artist` | `tpe1` |
| `title` | `tit2` |

**3.3 Conflit de valeurs (règle de non-perte).** Si deux noms d'une même famille portent
des valeurs **différentes**, les deux valeurs sont conservées comme **valeurs multiples de
la clé canonique** (une seule clé, N valeurs). Rien n'est écrasé.

> Comportement exécuté côté Android : `ALBUMARTIST=A` + `album_artist=B` →
> `albumartist=[A, B]`.

**3.4 Lecture des sources — Linux doit changer de lecteur.** `ffprobe` écrase les noms
concurrents (mesuré : la valeur A disparaît, §12). Linux lira la source avec **mutagen**
(bloc de commentaires brut, toutes les graphies conservées) ; `ffprobe` reste réservé aux
informations techniques (durée, débit, détection de la piste image). Android lit déjà via
jaudiotagger, qui expose tous les noms (mesuré : `VENDOR, TITLE, … ALBUMARTIST, …`).

Coût : lecture d'un bloc d'en-tête quelques kilo-octets, aucun décodage.

---

## 4. Liste fermée des clés écrites

`title`, `artist`, `album`, `albumartist`, `tracknumber`, `tracktotal`, `discnumber`,
`disctotal`, `date`, `genre`, `composer`, `comment`, `bpm`, `isrc`, `copyright`, `lyrics`,
**`label`** (voir arbitrage §12 décision 1), `catalognumber`, `barcode`.

Les champs ci-dessous ne sont **pas** des tags de données et sont exclus du contrat :

- `encoder` (généré par ffmpeg, absent côté Android, change à chaque version d'encodeur) ;
- `vendor` (chaîne d'en-tête du bloc commentaires : `Lavf58.76.100` vs `libopus` — jamais
  égalable) ;
- la **copie du champ `VENDOR` source en commentaire** : jaudiotagger l'expose comme un
  champ ordinaire et Android le recopie aujourd'hui (`vendor=reference libFLAC 1.3.0
  20130526`) → à ne plus recopier ;
- `REPLAYGAIN_*` : voir §7.

---

## 5. Critère d'identité d'un fichier

Deux sorties sont « identiques au contrat » quand :

- l'**ensemble de clés** est égal après normalisation en minuscules (§3.1) ;
- les **valeurs** de chaque clé sont égales (dans l'ordre, §2 principe 2) ;
- les **octets de l'image de couverture** sont égaux (§6).

Le **nombre** de clés ne doit pas différer : Linux ne doit plus écrire deux orthographes
du même champ (§11), Android ne doit plus copier `VENDOR`.

---

## 6. Couverture

- **Copie octet à octet** de la première image de la source. Récupérée par la lecture du
  bloc image existant (mutagen `pictures` / `APIC` côté Linux, `artworkList` côté Android)
  — copie mémoire, aucun décodage.
- **L'extraction par ffmpeg (`-frames:v 1`) est interdite** : elle ré-encode l'image
  (mesuré : 250 013 o → 42 007 o pour une même image 600×600, perte de qualité sans
  aucun gain).
- Bloc picture normalisé : `type = 3` (preuve), `mime` déduit de la signature
  (`\xFF\xD8` → jpeg, `\x89PNG` → png), **description vide** (arbitrage §12 décision 2),
  dimensions `0` (non contractuelles).
- Le test compare les **octets de l'image**, pas les champs du bloc picture.

---

## 7. ReplayGain (hors parité, mais encadré)

- **Aucune ressource dédiée** : jamais de passe de décodage **rajoutée pour** ces tags
  (§1). Linux écrit les siens à partir de la mesure que son auto-volume fait déjà (cache
  compris) ; Android, à partir de celle que son analyse en arrière-plan a déjà produite.
- **Règle d'écriture par côté (validée, §12 décision 3)** :
  - **Linux les écrit toujours** : la mesure fait partie de son pipeline auto-volume
    (activé par défaut), elle est mise en cache — donc payée au plus une fois par
    fichier — et le tag en est un produit dérivé. `--no-auto-volume` ne produit pas la
    mesure : pas de tag, et on **n'ajoute jamais de passe dédiée** pour les écrire (§1).
  - **Android les écrit seulement si le coût est marginal** : uniquement quand la
    mesure existe déjà au moment de la compression (cache rempli par l'analyse en
    arrière-plan — « comme pendant une compression »). Ni passe, ni déclenchement
    d'analyse rien que pour écrire un tag.
  - **Asymétrie assumée** : la présence diffère donc entre les deux côtés **par
    conception** — le test de parité **ne compare ni la présence ni la valeur**.
- Si ils sont écrits : clés exactes `REPLAYGAIN_TRACK_GAIN` (`+3.15 dB` format) et
  `REPLAYGAIN_TRACK_PEAK` (`0.681906`), référence ReplayGain 1.0, valeur brute de la
  mesure.
- **Source du gain, ordre strict** — une seule valeur est retenue par lecture :
  1. le cache `replaygain.json`, s'il contient une mesure pour ce fichier ;
  2. **sinon les tags du fichier** (`REPLAYGAIN_TRACK_GAIN`, `REPLAYGAIN_TRACK_PEAK`),
     qui sont alors **écrits dans le cache** : le fichier devient sa propre source, y
     compris pour un `.ogg` produit par Linux — et lanalyse est ainsi évitée ;
  3. sinon l'analyse en arrière-plan, comme aujourd'hui.

  Lire un tag coûte la lecture dun bloc den-tête (§1) : cest le repli le moins cher qui
  soit, et cest ce qui rend la règle compatible avec la contrainte de ressources.
- **Jamais de double application** : les tags ne sont appliqués **qu'en repli** (étape 2),
  jamais en plus dun gain déjà présent au cache ; la valeur retenue passe par la règle
  existante de lapplication (gain plafonné à lunité — jamais damplification), quelle que
  soit son origine.

---

## 8. Écriture — règles communes

1. **Clés en minuscules** des deux côtés (vorbis-java le fait déjà ; Linux écrira en
   minuscules via mutagen) → toute comparaison textuelle devient directe.
2. **Une seule entrée par clé canonique.** On n'écrit jamais un alias d'une clé déjà
   présente (toute graphie/nom confondus). C'est la correction du défaut de
   `copy_tags_with_cover` : la table d'alias est en minuscules alors que les clés
   recopiées sont mises en majuscules, ce qui laisse passer `ALBUM_ARTIST`, `TRACK`,
   `DISC`, `PUBLISHER` en plus de `ALBUMARTIST`, `TRACKNUMBER`, `DISCNUMBER`, `LABEL`.
3. **Ordre des commentaires : non contractuel** (les deux chaînes n'ordonnent pas pareil ;
   imposer l'ordre coûterait une réécriture gratuite sans bénéfice).
4. **Vendor** : conservé tel quel dans l'en-tête du bloc, jamais recopié comme commentaire.

---

## 9. Valeurs

- `trim()` des seuls espaces de bord.
- **Pas de découpe sur `;`** : une valeur reste une valeur (Linux le fait actuellement ;
  Android non → écart garanti sur toute valeur contenant un point-virgule).
- **Pas de déduplication**, pas de reformatage de dates, pas de conversion d'encodage
  (UTF-8 en entrée, UTF-8 en sortie).

---

## 10. Vérification

Comparaison champ par champ sur un échantillon de référence (source + sortie Linux +
sortie Android) :

- **inclus** : ensemble de clés (minuscules), valeurs par clé, octets de couverture ;
- **exclus** : `encoder`, `vendor`, `REPLAYGAIN_*`, ordre des clés, champs du bloc
  picture, débit audio, durée (tolérance existante).
- **attendu** : 0 écart.

Les scripts de comparaison existants (`compare.py`, `compare_file.py`) doivent appliquer
cette liste d'exclusion pour ne pas signaler d'écart non contractuel.

---

## 11. Checklist de mise en conformité

**Linux — `simplyconvert.py`**

- [ ] lecture des tags source via mutagen (toutes graphies) ; `ffprobe` pour le technique
      uniquement (§3.4)
- [ ] retrait du `split(";")` + `dedupe` dans `copy_tags_with_cover` (§9)
- [ ] comparaison de la table d'alias insensible à la casse **et** skip d'une clé déjà
      présente sous un autre nom (§8.2) — supprime les doublons `ALBUM_ARTIST`/`TRACK`/
      `DISC`/`PUBLISHER`
- [ ] clés écrites en minuscules (§8.1)
- [ ] clé `label` **ou** `publisher` seule, jamais les deux (§4, §12 décision 1)
- [ ] couverture : mutagen `pictures`/`APIC` en priorité, ffmpeg en dernier recours
      interdit pour les sources où l'image est lisible autrement (§6)

- [ ] `REPLAYGAIN_*` : **écriture systématique** quand la mesure existe — auto-volume
      activé (défaut), mesure en cache donc payée au plus une fois par fichier ;
      `--no-auto-volume` ne produit pas la mesure donc pas de tag, et on nen ajoute
      **jamais de passe dédiée** (§7, §12 décision 3)

**Android — `OpusTags.kt`, `CompressionFlow.kt`**

- [ ] ne plus recopier le champ `VENDOR` de la source comme commentaire (§4)
- [ ] écrire `label` pour les entrées Vorbis si l'arbitrage §12 décision 1 tranche pour `label`
      (aujourd'hui : `PUBLISHER` en FLAC, `LABEL` en MP3 — asymétrie interne)
- [ ] couverture : inchangée (déjà octet à octet), description vidée (§6)
- [ ] `REPLAYGAIN_*` : **écriture seulement si coût marginal** — la mesure existe déjà
      au moment de la compression (cas actuel, inchangé) ; ni passe ni déclenchement
      d'analyse rien que pour un tag (§7, §12 décision 3). Exclu des tests (§7), **mais**
      repli à implémenter : si
      `replaygain.json` na pas dentrée pour le fichier, lire les tags du fichier, écrire
      la valeur lu dans le cache, et ne déclencher lanalyse quen dernier recours (§7).
      NOTE : `ReplayGain` nexpose aujourd hui que `gainFor` / `peakFor` / `hasAnalysis` /
      `requestAnalysis` / `prune` / `clear` — il faut y ajouter une écriture denrée ; la
      lecture des tags passe par le lecteur Vorbis déjà en place (`VorbisTagReader` / la
      voie vorbis-java d`OpusTags`, jaudiotagger ne lisant pas lOpus)

---

## 12. Mesures à l'appui et décisions validées

**Observations (échantillon `12 - Hand in Your Notice`, mesuré le 06/10/2026)**

| fait | mesure |
|---|---|
| Linux écrit deux orthographes par champ | `ALBUMARTIST` + `ALBUM_ARTIST`, `TRACKNUMBER` + `TRACK`, `DISCNUMBER` + `DISC`, `LABEL` + `PUBLISHER` |
| clés écrites en casse différente | Linux majuscules (`PUBLISHER`, `LENGTH`), Android minuscules (`publisher`, `length`) |
| couverture | Linux 42 007 o (ré-encodée), Android 250 013 o (source) |
| champ parasite | Android `vendor=reference libFLAC 1.3.0 20130526` (jaudiotagger expose `VENDOR`) ; Linux `encoder=Lavc58.134.100 libopus` |
| valeurs communes | 20 champs identiques entre Linux et Android |
| débit réel (même cible 160 kbps) | Linux 171 951 bps, Android 185 255 bps — hors contrat |
| ReplayGain | Linux toujours (`+3.15 dB`), Android seulement si analysé avant → non déterministe |
| ffprobe écrase les noms concurrents | `ALBUMARTIST=A` + `album_artist=B` → ffprobe ne rend que `B` ; mutagen et jaudiotagger rendent `A` et `B` |
| écriture Android | `addComment`/`removeComments` passent par `normaliseTag` (`toLowerCase`) → 1 clé, N valeurs, jamais de doublon de casse (test exécuté, EXIT=0) |

**Décisions arbitraires — validées le 06/10/2026**

1. ✅ **`label` vs `publisher`** — **validée : `label`** (conforme Picard et au mapping ID3
   `TPUB` déjà utilisé côté Android pour les MP3). Conséquence : Linux arrête d'écrire
   `LABEL` en plus de `PUBLISHER`, Android renomme `PUBLISHER` → `label` sur les entrées
   FLAC. Alternative écartée : garder `publisher` (moins de changements côté Android).
2. ✅ **Description du bloc picture** — **validée : vide** (comportement Android actuel).
   Alternative écartée : `Cover` (comportement Linux actuel).
3. ✅ **ReplayGain** — **validée : Linux les écrit toujours, Android seulement si le coût
   est marginal** (mesure déjà disponible au moment de la compression, cache rempli par
   l'analyse en arrière-plan). Jamais de passe dédiée des deux côtés (§1). Reste exclu du
   test de parité (§7) : la présence diffère par conception.
4. ✅ **Casse des clés** — **validée : minuscules** (§8.1). Alternative écartée : majuscules,
   où Android dépendrait de la normalisation de vorbis-java et où toute comparaison
   textuelle brute devrait normaliser.
