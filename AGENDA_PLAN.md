# Socle « calendrier unifié » — plan collecteur (agenda_events)

But : donner à La Régie **tous** les événements de tes agendas TimeTree (pas seulement
les concours) et lui permettre de **poser** un événement dans le calendrier voulu, avec
ou sans horaire — le tout via le pont web non officiel déjà en place (`timetree_exporter`
pour lire, la classe `TimeTree` de `timetree_ecrire.py` pour écrire).

La table partagée `agenda_events` est créée côté La Régie (migration
`supabase/migrations/0068_agenda_events.sql`, à lancer sur Supabase). Ce plan ne concerne
que le **collecteur**. Ensuite La Régie code le bouton « 📅 Planifier » (autre conversation).

---

## 0. La table (rappel — déjà définie côté La Régie)

`agenda_events` : `id, timetree_uid (unique), calendrier, titre, debut (timestamptz),
fin, journee (bool), lieu, categorie, mail_id, source ('import'|'planif'),
a_ecrire (bool), a_supprimer (bool), vu_le, cree_le`.

Accès depuis le collecteur : `ts.requete(env, "GET"|"POST"|"PATCH"|"DELETE", "agenda_events?…", corps)`
(comme pour les autres tables).

## 1. LECTURE — importer tous les événements (`source='import'`)

Aujourd'hui `timetree_sync.py` lit les `.ics` mais **jette** tout ce qui n'est pas un
concours (journées entières multi-jours). On veut **aussi** stocker le reste.

Nouvelle passe (dans `timetree_sync.py`, ou un `agenda_sync.py` appelé après l'export),
pour **chaque** `.ics` exporté (on connaît le nom du calendrier via l'index de
`timetree_export.py`) :

1. `for ev in lire_ics(texte):` — réutilise le parseur existant. Chaque VEVENT donne
   `UID, SUMMARY, DTSTART, DTEND, LOCATION`.
2. En extraire :
   - `timetree_uid` = UID,
   - `titre` = SUMMARY,
   - `journee` = vrai si `DTSTART` porte `VALUE=DATE` (pas d'heure),
   - `debut` = DTSTART (date → minuit local ; datetime → tel quel, en ISO 8601),
   - `fin` = DTEND si présent (TimeTree met la fin exclusive pour les journées entières :
     garder tel quel, La Régie gère l'affichage),
   - `lieu` = LOCATION (ou null),
   - `calendrier` = le nom du calendrier courant,
   - `source` = `'import'`, `vu_le` = maintenant.
3. **Upsert** par `timetree_uid` :
   `ts.requete(env, "POST", "agenda_events?on_conflict=timetree_uid", [ligne], prefer="resolution=merge-duplicates,return=minimal")`.
   ⚠️ Ne pas écraser une ligne `source='planif'` non encore poussée (elle n'a pas encore
   d'`uid`) — l'upsert par `timetree_uid` ne la touche pas, c'est OK.
4. **Purge** : à la fin d'un import complet, supprimer les `import` disparus de TimeTree :
   `DELETE agenda_events?source=eq.import&vu_le=lt.<début_de_cette_passe>`
   (ceux qu'on n'a pas revus cette fois ont été supprimés côté TimeTree).

> Concours : ils sont aussi des événements du calendrier « D'clik agency » → ils
> entreront dans `agenda_events`. C'est voulu (la vue calendrier les montrera). Le
> rapprochement avec la table `evenements` (pour les actions concours) se fera côté
> front par `timetree_uid`. Ne rien changer au sync concours existant.

## 2. ÉCRITURE — poser un événement planifié (`source='planif'`)

Nouvelle passe (dans `timetree_ecrire.py`, après le traitement de `ecritures_timetree`) :

1. **Créer** — lignes à pousser :
   `GET agenda_events?source=eq.planif&a_ecrire=eq.true&timetree_uid=is.null`
   Pour chacune :
   - `cal_id = tt.calendrier(ligne["calendrier"])` — résout le calendrier par nom
     (déjà présent dans la classe `TimeTree`). S'il est introuvable → marquer une erreur
     et passer (ne pas boucler).
   - Construire le corps (voir §3), `uuid = tt.creer(cal_id, corps)`.
   - `PATCH agenda_events?id=eq.<id>` → `{timetree_uid: uuid, a_ecrire: false}`.
2. **Supprimer** — lignes à retirer :
   `GET agenda_events?a_supprimer=eq.true&timetree_uid=not.is.null`
   Pour chacune : `tt.supprimer(tt.calendrier(ligne["calendrier"]), ligne["timetree_uid"])`
   puis `DELETE agenda_events?id=eq.<id>`.

Réutiliser la connexion `TimeTree` existante (une seule ouverture de session).

## 3. Le corps d'événement (all-day vs horaire)

À côté de `contenu(ev)` (spécifique concours), ajouter un builder pour `agenda_events` :

```python
def contenu_agenda(row):
    base = {"title": row["titre"], "category": 1, "attendees": [], "alerts": [],
            "recurrences": [], "file_uuids": []}
    if row["lieu"]:
        base["location"] = row["lieu"]
    if row["journee"]:
        # journée entière : minuit UTC du premier et du dernier jour (comme contenu()).
        d0 = row["debut"][:10]
        d1 = (row.get("fin") or row["debut"])[:10]
        base.update(all_day=True, start_at=minuit_utc_ms(d0), start_timezone="UTC",
                    end_at=minuit_utc_ms(d1), end_timezone="UTC")
    else:
        # avec horaire : epoch ms, fuseau Europe/Paris ; fin = début + 1h si absente.
        start_ms = iso_to_ms(row["debut"])
        end_ms   = iso_to_ms(row["fin"]) if row.get("fin") else start_ms + 3600_000
        base.update(all_day=False, start_at=start_ms, start_timezone="Europe/Paris",
                    end_at=end_ms, end_timezone="Europe/Paris")
    return base
```

`tt.creer()` ajoute déjà `category/attendees/alerts/recurrences/file_uuids` — vérifier
qu'il ne double pas ces clés (sinon ne les mettre que dans l'un des deux).
Ajouter un petit `iso_to_ms(iso)` (parse ISO 8601 → millisecondes epoch).

## 4. Où brancher dans GitHub Actions

- **Import (§1)** : après l'export `.ics`, dans le même job que `timetree_sync` (workflow
  de collecte). Tourne à chaque passage.
- **Écriture (§2)** : dans le job `timetree-ecrire.yml`, juste après le traitement de
  `ecritures_timetree` (La Régie le déclenche déjà via `/api/refresh {quoi:"timetree"}`).

## 5. Tests

- `lire_ics` d'un `.ics` avec un événement daté (heure) et un journée-entière → les deux
  arrivent en base avec `journee` correct.
- Purge : un uid absent du nouvel import est supprimé ; un `planif` sans uid est épargné.
- Écriture : une ligne `planif, a_ecrire=true` → `tt.creer` appelé avec le bon `cal_id`,
  puis `timetree_uid` renseigné et `a_ecrire=false`.
- Suppression : `a_supprimer=true` → `tt.supprimer` puis ligne supprimée.
- Corps : journée entière = minuit UTC ; horaire = epoch ms + Europe/Paris.

## 6. Déploiement

Comme d'habitude : push sur le repo `regie-collecteur` (les workflows GitHub Actions
prennent la version `main`). Vérifier ensuite qu'`agenda_events` se remplit (LECTURE) et
qu'une ligne `planif` de test est bien créée dans TimeTree (ÉCRITURE).

---

## Récap des changements collecteur

| Fichier | Changement |
|---|---|
| `timetree_sync.py` (ou `agenda_sync.py`) | passe LECTURE : parse tous les VEVENT → upsert `agenda_events` (source='import') + purge |
| `timetree_ecrire.py` | passe ÉCRITURE : `planif` a_ecrire→`tt.creer`, a_supprimer→`tt.supprimer` ; + `contenu_agenda()` + `iso_to_ms()` |
| workflows | import dans le job de collecte ; écriture dans `timetree-ecrire.yml` |
| tests | lecture datée/journée, purge, écriture, suppression, corps |

Côté La Régie (autre conversation, après ce socle) : migration `0068_agenda_events.sql`
lancée sur Supabase, puis le bouton « 📅 Planifier » sur un mail (IA → formulaire →
insert `agenda_events(source='planif', a_ecrire=true)` → `/api/refresh {quoi:"timetree"}`),
et plus tard la grille mois qui lit `agenda_events` + les concours.
