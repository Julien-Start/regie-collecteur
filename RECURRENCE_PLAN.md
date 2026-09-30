# Déplier les récurrences dans agenda_events — plan collecteur

But : aujourd'hui l'import (`evenements_agenda` dans `timetree_sync.py`) ne garde **qu'une
occurrence** des événements récurrents (garde `👨‍👦‍👦`, entraînements, etc.). On veut
**déplier chaque récurrence** en occurrences individuelles dans `agenda_events`, pour que :
- la garde et les événements récurrents apparaissent partout dans le calendrier de La Régie,
- La Régie puisse lire les **vraies semaines de garde** (`👨‍👦‍👦`) et y poser le judo.

Ne concerne que le collecteur. Ne touche pas au reste (concours, écriture planif).

---

## 1. Où

Dans **`timetree_sync.py`**, fonction **`evenements_agenda(nom_calendrier, texte, aujourdhui)`**
(la boucle `for ev in lire_ics(texte)`). Chaque VEVENT donne aussi une propriété **`RRULE`**
(et parfois `EXDATE`, `RECURRENCE-ID`) via `lire_ics` : `ev.get("RRULE")` renvoie `(params, valeur)`.

## 2. Ce qu'il faut faire, par VEVENT

1. Extraire comme aujourd'hui : `uid`, `debut/journee` (`moment_ics(DTSTART)`), `fin` (`moment_ics(DTEND)`), titre, lieu.
2. **Durée** de l'événement = `fin - debut` (garder pour l'appliquer à chaque occurrence).
3. Si **pas de `RRULE`** → une seule ligne, comme aujourd'hui.
4. Si **`RRULE` présent** → déplier :
   - Fenêtre : de `aujourdhui - 90 j` à **`2027-07-31`** (couvre la vue + le judo jusqu'à juin 2027). Un plafond de sécurité (ex. 500 occurrences max) évite l'emballement.
   - Utiliser **`dateutil.rrule.rrulestr`** : `rrulestr(valeur_rrule, dtstart=<datetime de debut>)`, puis itérer `.between(fenetre_debut, fenetre_fin, inc=True)` (respecte `FREQ`, `INTERVAL`, `BYDAY`, `UNTIL`, `COUNT`).
   - Exclure les dates de **`EXDATE`** (occurrences supprimées).
   - Pour **chaque occurrence** `occ` : `debut = occ`, `fin = occ + durée`, mêmes `titre/lieu/journee/calendrier`, `source='import'`, et un **uid synthétique unique** :
     `timetree_uid = "<uid_maître>#" + occ.date().isoformat()` (sinon l'upsert `on_conflict=timetree_uid` écraserait toutes les occurrences sur une seule ligne).
5. **`RECURRENCE-ID` (occurrences modifiées)** : TimeTree peut exporter une occurrence déplacée comme VEVENT séparé (avec `RECURRENCE-ID`). Simple et suffisant pour un premier jet : on la laisse arriver comme un événement normal (son propre uid) ; si un doublon visuel apparaît sur cette date, on affinera en sautant l'occurrence générée par le maître à cette date. À noter, pas bloquant.

## 3. Dépendance

Ajouter **`python-dateutil`** à l'install pip des workflows qui lancent `timetree_sync.py`
(`timetree.yml` et `timetree-ecrire.yml`) : `pip install --quiet timetree-exporter python-dateutil`.

## 4. Purge

Inchangée : `importer_agenda` supprime déjà les `source='import'` d'un calendrier non revus
(`vu_le < début de passe`). Les occurrences dépliées (uid `maître#date`) sont revues à chaque
passe tant qu'elles tombent dans la fenêtre → elles persistent ; celles qui sortent de la
fenêtre ou disparaissent de TimeTree sont purgées. ✅

## 5. Tests

- Un VEVENT `FREQ=WEEKLY;INTERVAL=2` → occurrences toutes les 2 semaines dans la fenêtre, uid distincts.
- `EXDATE` → l'occurrence exclue n'apparaît pas.
- Événement non récurrent → une seule ligne (inchangé).
- Durée conservée (un bloc garde d'une semaine reste d'une semaine sur chaque occurrence).
- Plafond de sécurité respecté.

## 6. Déploiement

Push sur `regie-collecteur`. Puis relancer (ou `gh workflow run timetree-ecrire.yml`) et vérifier
dans `agenda_events` que la garde `👨‍👦‍👦` a **plusieurs** occurrences (une par semaine de garde).

---

## Récap

| Fichier | Changement |
|---|---|
| `timetree_sync.py` · `evenements_agenda()` | si `RRULE` → déplier en occurrences (dateutil), uid `maître#date`, exclure `EXDATE` |
| `timetree.yml` / `timetree-ecrire.yml` | ajouter `python-dateutil` au pip install |
| `tests` | récurrence bi-hebdo, EXDATE, non-récurrent, durée, plafond |

Côté La Régie (moi, après ce plan) : une fois la garde `👨‍👦‍👦` dépliée, je génère le **judo
du mercredi 16h30–18h15**, uniquement les semaines de garde, **hors vacances zone B**, jusqu'à
**juin 2027** (je confirmerai les dates de vacances avant de créer).
