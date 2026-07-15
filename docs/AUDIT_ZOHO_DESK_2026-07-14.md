# Audit Zoho Desk rule-aware - 14 juillet 2026

## Statut du document

Cette version remplace integralement le premier scoring du 14 juillet. Le premier audit evaluait trop severement les reponses Uber D/E en leur reprochant de ne pas traiter l'intention courante. D/E sont des warnings pendant l'analyse, puis leur rendu devient volontairement terminal et masque les intentions. Les verdicts ci-dessous ont ete recalcules avec cette priorite et avec les valeurs CRM historiques a l'heure de chaque envoi.

## Decision

- Ne pas reactiver l'auto-envoi en production.
- Les cas Uber D/E ne sont pas la source principale des erreurs : les 4 cas observes sont conformes au rendu terminal code.
- Les anomalies certaines concernent surtout les reports, la duree des sessions du soir, le triage, Uber B et les contradictions de paiement.
- Le seul lot immediatement automatisable est la cloture sans reponse des notifications directes no-reply.
- Les reponses clients doivent rester en brouillon ou en mode fantome jusqu'a validation d'au moins 200 cas par couple exact `etat x intention`.

Les correctifs de securite presents dans le depot sont locaux et non deployes par cet audit.

## Methode

L'audit a utilise uniquement des appels GET Zoho Desk/CRM. Aucun workflow, agent LLM, pseudo dry-run ou linker n'a ete execute.

Pour chaque reponse :

1. recuperation du thread Desk entrant et du thread sortant ;
2. liaison au deal par `cf_opportunite`, sans rejouer le linker ;
3. recuperation paginee des notes et de la timeline CRM ;
4. selection de la note `[META] ticket=<id>` anterieure a l'envoi ;
5. rollback des champs CRM modifies apres l'envoi ;
6. recalcul du cas Uber J+4 ;
7. application des priorites et templates reellement executes ;
8. contre-revue independante des violations et des conformites.

Le rapport Markdown ne contient ni secret ni identite candidat. Le dataset technique temporaire a ete supprime apres l'audit car l'expurgation avait laisse passer deux valeurs sensibles.

## Perimetre

| Mesure | Volume |
|---|---:|
| Tickets fermes observes, tous departements | 14 948 |
| Tickets DOC fermes observes | 4 306 |
| Pages Desk lues | 222 |
| Tickets avec le sujet historique auto-send | 36 |
| Reponses attribuees a l'automatisation | 32 |
| Reponses agent uniquement | 4 |
| Reponses auto envoyees dans la fenetre stricte de 30 jours | 28 |
| Conversations d'au moins 3 threads examinees | 387 |
| Brouillons auto exploitables | 8 |
| Pool de notifications systeme | 2 433 |
| Notifications echantillonnees | 120 |

La liste Desk n'est pas strictement ordonnee par date de fermeture. Les volumes sont donc des volumes observes, pas une garantie d'exhaustivite. Quatre des 32 reponses auto sont anciennes mais appartiennent a des tickets fermes pendant la fenetre : `1179730`, `1207816`, `1115961`, `1112866`.

## Regles appliquees

### Branches sans reponse client

- ticket deja ferme ou brouillon existant ;
- spam, bounce, notification CMA/no-reply ;
- routage sans accuse specifique ;
- aucun deal exploitable ;
- escalade d'une annulation insistante.

### Remplacements volontaires

- doublon Uber, clarification de doublon, reprise de dossier ;
- candidat introuvable ;
- refus primaire de partager les credentials ;
- perte d'acces ExamT3P ;
- Uber D et E : bloc Uber uniquement, intentions et autres sections masquees ;
- Uber A : masque la plupart des intentions ;
- Uber B : ajoute l'alerte test mais ne remplace pas l'intention ;
- report bloque : ne doit pas proposer ou confirmer librement une nouvelle date.

### Regles de coherence utilisees

- une session du soir de 40 h dure deux semaines, 18 h-22 h ;
- une session doit finir avant l'examen ;
- `Pret a payer` signifie dossier complet, mais pas inscription definitivement validee ;
- `EXAM_INCLUS=Oui` signifie que CAB paie les frais ;
- `Dossier Synchronise` ne signifie pas `VALIDE CMA` ;
- une date mentionnee par le candidat n'ecrase pas la date CRM, mais une divergence importante doit etre expliquee ;
- credentials en clair : conformes a certains templates historiques, mais traites comme risque securite separe.

## Resultats corriges

### Ensemble des 32 envois

| Verdict | Nombre | Part |
|---|---:|---:|
| Conforme | 8 | 25,0 % |
| Conforme avec defaut mineur | 9 | 28,1 % |
| Violation majeure | 9 | 28,1 % |
| Violation critique | 2 | 6,3 % |
| Non verifiable | 4 | 12,5 % |

Les violations materielles representent 11/32, soit 34,4 %. Les reponses conformes ou conformes avec defaut mineur representent 17/32, soit 53,1 %.

### Fenetre stricte des 28 envois recents

| Verdict | Nombre | Part |
|---|---:|---:|
| Conforme | 7 | 25,0 % |
| Conforme avec defaut mineur | 8 | 28,6 % |
| Violation majeure | 7 | 25,0 % |
| Violation critique | 2 | 7,1 % |
| Non verifiable | 4 | 14,3 % |

Les violations materielles representent 9/28, soit 32,1 %.

## Detail des 32 cas

| Ticket | Verdict | Regle ou constat determinant |
|---|---|---|
| `1228523` | Mineur | Etat convocation/session passee correct ; instruction circulaire ayant provoque une contestation. |
| `1222601` | Conforme | Rendu Uber D terminal ; `Compte_Uber=false` a l'heure de l'envoi. |
| `1226721` | Conforme | Demande explicite d'identifiants, etat/date conformes. |
| `1226505` | Conforme | Session du soir et examen coherents, reponse directe. |
| `1226489` | Mineur | Rendu Uber D terminal correct ; META de date devenu obsolete. |
| `1225763` | Non verifiable | Template acces perdu coherent, mais echec de connexion historique non conserve. |
| `1225431` | Majeur | Conflit code/donnee : `EXAM_INCLUS=Non`, mais reponse annonce prise en charge totale et lien de paiement. |
| `1224933` | Mineur | Rendu Uber E terminal correct ; META de date obsolete. |
| `1224479` | Majeur | Incident de cours classe en probleme documentaire ; communication avec la formatrice ignoree. |
| `1224443` | Mineur | Rendu Uber E terminal correct ; trace META obsolete sans effet sur la reponse. |
| `1179730` | Mineur | Report/session globalement coherents, mais statut de l'action CMA ambigu. Ancien. |
| `1223994` | Mineur | Comportement code du lien visio respecte, mais demande non resolue et instruction circulaire. |
| `1222900` | Mineur | Session, convocation et examen concordants ; affirmation d'absence d'alternative non verifiable sans catalogue historique. |
| `1222767` | Non verifiable | Message entrant vide ; intention et pertinence non demonstrables. |
| `1221769` | Non verifiable | Piece jointe absente de l'extraction. |
| `1220675` | Majeur | `Pret a payer` presente comme inscription confirmee et `EXAM_INCLUS=Non` presente comme pris en charge. |
| `1221594` | Majeur | Uber B exigeait le test ; la reponse affirme au contraire que le test est reussi. |
| `1220012` | Majeur | 40 h de cours du soir annoncees sur cinq soirees, soit seulement 20 h. |
| `1217575` | Conforme | Envoi d'identifiants avant J+4 ; session et examen coherents. |
| `1218045` | Mineur | Date CRM correcte, mais changement depuis le 30/06 et lieu non expliques. |
| `1217807` | Conforme | Identifiants et statut `Pret a payer` coherents. |
| `1217375` | Conforme | Identifiants, session et date coherents. |
| `1207816` | Majeur | Session proposee apres un examen declare non modifiable. Ancien. |
| `1212076` | Majeur | Meme incoherence de 40 h sur cinq soirees. |
| `1211968` | Mineur | Date CRM correcte mais divergence avec la date citee non expliquee. |
| `1210167` | Conforme | Template code du lien visio respecte ; relance operationnelle ulterieure. |
| `1115961` | Conforme | Confirmation de session et mise a jour CRM concordantes. Ancien. |
| `1209345` | Non verifiable | Etat/date coherents ; catalogue des autres dates non conserve. |
| `1209266` | Majeur | Convocation recue et report bloque, mais dates alternatives proposees sans force majeure. |
| `1209264` | Critique | Chronologie absence/formation/examen materiellement fausse. |
| `1209262` | Critique | Examen du 30/06 presente comme anterieur a un depart le 24/06. |
| `1112866` | Majeur | Message incomprehensible non classe `MESSAGE_CONFUS`, puis reponse dossier detaillee. Ancien. |

## Correction Uber D/E

Distribution historique :

| Cas Uber | Nombre | Resultat |
|---|---:|---|
| ELIGIBLE | 27 | Melange de conformites et violations |
| D | 2 | 2 conformes ou mineurs |
| E | 2 | 2 conformes ou mineurs |
| B | 1 | 1 violation majeure |

Les tickets `1226489` et `1222601` etaient donc de mauvais exemples initiaux. Leur rendu terminal est conforme au runtime. Cette correction est integree au scoring final.

## Causes principales

### Reports et chronologie

- `1209266` : report bloque mais alternatives proposees ;
- `1209264` et `1209262` : chronologies fausses, severite critique ;
- `1207816` : formation proposee apres l'examen ;
- `1209345` : non verifiable sans catalogue historique.

### Sessions du soir

`1220012` et `1212076` annoncent une formation de 40 h du lundi au vendredi, 18 h-22 h. Cela ne represente que 20 h et contredit la regle codee des deux semaines.

### Triage

- `1224479` : incident pedagogique classe en documents ;
- `1112866` : message incomprehensible non classe `MESSAGE_CONFUS`.

### Uber B

`1221594` devait rappeler le test de selection manquant. La reponse affirme qu'il etait deja reussi.

### Paiement

`1225431` et `1220675` exposent une contradiction interne au code : le partial `Pret a payer` utilise `uber_20` pour annoncer la prise en charge, alors que les variables centrales utilisent `EXAM_INCLUS == Oui` comme source de verite.

## Brouillons automatiques

Huit brouillons ont pu etre analyses :

| Issue | Nombre |
|---|---:|
| Violation certaine | 3 |
| Remplace ou abandonne par un agent | 4 |
| Conforme et non envoye car simple remerciement | 1 |

Violations certaines :

- `1061154` : attente de resultats alors que le candidat signale absence et convocation non recue ;
- `1219381` : report confirme sans droit ni validation ;
- `1223994` : « nous venons de renvoyer le lien » sans action attestee.

Le brouillon `1228276` n'est pas une violation : Uber A masque volontairement la demande Paris/Nantes. Il a neanmoins ete remplace par une reponse humaine plus ciblee.

## Notifications systeme

Le pool contient 2 433 notifications aux sujets ExamT3P/CMA connus. Un echantillon pseudo-aleatoire de 120 tickets a ete controle :

- 120/120 provenaient du domaine `exament3p.fr` avec un sender classe `noreply`, `no-reply` ou `notification` ;
- 120/120 possedaient un brouillon automatique ;
- 120/120 utilisaient le meme fallback candidat introuvable demandant nom, email et telephone ;
- 0/120 auraient du recevoir un brouillon selon la regle codee.

Seule l'existence de 120 cas est certaine. Une projection aux 2 433 tickets serait fragile, meme si le taux observe est de 100 %. Le defaut est de severite elevee mais les brouillons n'ont pas ete envoyes automatiquement.

Cause fortement indiquee par le code et plusieurs tickets inspectes : Zoho peut fournir un sender avec nom affiche, tandis que le filtre historique comparait la chaine brute a l'adresse seule. Le dataset agrege ne conservait pas l'adresse exacte des 120 cas et ne permet pas d'affirmer que les 120 avaient strictement la meme forme.

## Securite et redaction

Ces signaux sont separes de la conformite metier, car certains sont explicitement codes :

- 25/32 reponses contenaient un bloc identifiant/mot de passe ou une consigne associee ;
- 21/32 contenaient ce bloc hors intention directe de demande/envoi d'identifiants ;
- 4/32 messages entrants contenaient des credentials ;
- 31/32 reponses depassaient 140 mots ;
- 11/32 tickets ont recu une relance sous 72 h, dont plusieurs relances attendues ou simples demandes de rappel.

Un bloc credential ne prouve pas toujours qu'un secret personnel etait affiche. Il reste toutefois incompatible avec un futur auto-envoi prudent.

## Qualite des preuves

| Preuve | Disponibilite |
|---|---:|
| Deal lie et snapshot CRM historique | 32/32 |
| META selectionne | 32/32 |
| Notes completes | 32/32 |
| Timeline complete | 27/32 |
| Entree textuelle non vide | 31/32 |
| Piece jointe signalee mais contenu absent | 5/32 |

Anomalies de trace :

- date META differente du CRM historique : 5 cas ;
- session META differente du CRM : 2 cas confirmes ;
- plusieurs META candidates : 14 cas ;
- intention `N/A` : 1 cas ;
- valeurs `Compte_Uber` ou `ELIGIBLE` parfois stockees comme chaines `"false"`, dangereuses avec la verite Python directe.

## Automatisation recommandee

### Production

Allowlist vide. Aucun segment ne justifie encore un envoi actif.

### Mode fantome uniquement

Premier segment a observer :

```text
etat = VALIDE_CMA_WAITING_CONVOC
intention = DEMANDE_DATE_VISIO
cas Uber = ELIGIBLE
```

Conditions minimales :

- META unique et complet ;
- timeline complete ;
- booleens CRM stricts, pas chaines ;
- date et session identiques entre META et CRM ;
- session future, non commencee, finissant avant l'examen ;
- aucun report, absence, force majeure ou changement ;
- aucune intention secondaire ;
- aucun credential, email personnel ou piece jointe ;
- reponse de moins de 120 mots ;
- aucune erreur ou warning de validation.

Le seul ticket historique proche de cette porte est `1226505`. Un seul exemple est insuffisant.

### Deuxieme candidat fantome

Le rendu terminal Uber D/E a produit 4/4 reponses conformes. Comme il s'agit d'une decision defavorable, le lot doit rester en validation humaine jusqu'a au moins 200 cas controles, avec verification historique stricte de `Compte_Uber`, `ELIGIBLE` et J+4.

### Automatisation sans reponse client

La cloture des notifications exactes no-reply est le lot le plus sur. Le correctif local normalise l'adresse avant le lookup CRM et bloque le brouillon.

## Correctifs locaux deja prepares

- auto-envoi desactive par defaut ;
- allowlist `AUTO_SEND_SCENARIOS` vide ;
- futurs scenarios limites a un tuple exact sujet/intention/etat ;
- controle du statut, departement et dernier thread ;
- coalescence des evenements webhook concurrents ;
- normalisation des senders no-reply ;
- authentification fail-closed ;
- `/webhook/test` desactive par defaut ;
- 35 tests de securite ajoutes.

Verification : 107 tests selectionnes passent. La collecte globale historique reste cassee par plusieurs anciens tests qui remplacent et ferment `sys.stdout`.

## Limites finales

- le SHA exact deploye n'est pas inscrit dans les META ;
- la timeline CRM est incomplete pour 5 cas ;
- les snapshots historiques ExamT3P et catalogues sessions/dates ne sont pas conserves ;
- l'auteur Desk seul ne constitue pas une preuve absolue d'automatisation, meme si sujet, compte auteur et comportement concordent ;
- le Humanizer rend le texte final non deterministe ;
- les verdicts `Non verifiable` ne doivent pas etre comptes comme erreurs ni conformites.
