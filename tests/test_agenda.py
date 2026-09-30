#!/usr/bin/env python3
# Le calendrier unifié (agenda_events) : lecture des .ics, purge, écriture dans TimeTree.
#   python3 tests/test_agenda.py
# Stdlib uniquement. Rien ne sort sur le réseau : Supabase et TimeTree sont simulés.
import os, sys, types, unittest
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# timetree_ecrire importe requests et timetree-exporter : inutiles pour ces tests.
for nom, attributs in (("requests", {"Session": object}),
                       ("timetree_exporter", {}),
                       ("timetree_exporter.api", {}),
                       ("timetree_exporter.api.auth", {"login": lambda *a, **k: ""}),
                       ("timetree_exporter.api.calendar", {"TimeTreeCalendar": object})):
    if nom not in sys.modules:
        mod = types.ModuleType(nom)
        for k, v in attributs.items():
            setattr(mod, k, v)
        sys.modules[nom] = mod

import timetree_sync as ts
import timetree_ecrire as te

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:aaa-journee
SUMMARY:État des lieux Le Mans
DTSTART;VALUE=DATE:%(j1)s
DTEND;VALUE=DATE:%(j2)s
LOCATION:Le Mans
END:VEVENT
BEGIN:VEVENT
UID:bbb-horaire
SUMMARY:RDV comptable
DTSTART;TZID=Europe/Paris:%(j1)sT170000
DTEND;TZID=Europe/Paris:%(j1)sT180000
END:VEVENT
BEGIN:VEVENT
UID:ccc-vieux
SUMMARY:Concours 2019
DTSTART;VALUE=DATE:20190501
END:VEVENT
END:VCALENDAR
"""


def ics_du_jour():
    aujourdhui = date.today()
    return ICS % {"j1": aujourdhui.strftime("%Y%m%d"),
                  "j2": aujourdhui.strftime("%Y%m%d")}


class FausseRegie:
    """Supabase simulé : garde les appels pour qu'on les vérifie."""
    def __init__(self, reponses=None):
        self.appels = []
        self.reponses = reponses or {}

    def requete(self, env, methode, chemin, corps=None, prefer=None):
        self.appels.append((methode, chemin, corps))
        for cle, valeur in self.reponses.items():
            if cle in chemin and methode == "GET":
                return valeur
        return None


class FauxTimeTree:
    def __init__(self, calendriers):
        self.calendriers = calendriers
        self.crees, self.supprimes = [], []

    def calendrier(self, nom):
        if nom not in self.calendriers:
            raise RuntimeError("calendrier introuvable dans TimeTree")
        return self.calendriers[nom]

    def creer(self, cal_id, corps):
        self.crees.append((cal_id, corps))
        return "uuid-%d" % len(self.crees)

    def supprimer(self, cal_id, uuid):
        self.supprimes.append((cal_id, uuid))


class Lecture(unittest.TestCase):
    def test_journee_et_horaire(self):
        lignes = ts.evenements_agenda("Privé", ics_du_jour())
        self.assertEqual(len(lignes), 2, "l'événement de 2019 est hors fenêtre")
        par_uid = {l["timetree_uid"]: l for l in lignes}
        journee = par_uid["aaa-journee"]
        self.assertTrue(journee["journee"])
        self.assertTrue(journee["debut"].endswith("T00:00:00+00:00"))
        self.assertEqual(journee["lieu"], "Le Mans")
        self.assertEqual(journee["calendrier"], "Privé")
        self.assertEqual(journee["source"], "import")
        horaire = par_uid["bbb-horaire"]
        self.assertFalse(horaire["journee"])
        self.assertIn("T17:00:00", horaire["debut"])
        self.assertIn("T18:00:00", horaire["fin"])
        self.assertIsNone(horaire["lieu"])

    def test_upsert_et_purge(self):
        faux = FausseRegie()
        ancien = ts.requete
        ts.requete = faux.requete
        try:
            ts.importer_agenda({}, [("Privé", ics_du_jour())])
        finally:
            ts.requete = ancien
        posts = [a for a in faux.appels if a[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn("on_conflict=timetree_uid", posts[0][1])
        self.assertEqual(len(posts[0][2]), 2)
        self.assertTrue(all(l.get("vu_le") for l in posts[0][2]))
        suppr = [a for a in faux.appels if a[0] == "DELETE"]
        self.assertEqual(len(suppr), 1)
        # la purge ne touche que les 'import' de CE calendrier, pas les 'planif'
        self.assertIn("source=eq.import", suppr[0][1])
        self.assertIn("calendrier=eq.Priv", suppr[0][1])
        self.assertIn("vu_le=lt.", suppr[0][1])


class Recurrences(unittest.TestCase):
    """Un récurrent (garde, entraînement) vaut une ligne par occurrence."""
    AUJOURDHUI = date(2026, 10, 1)

    def ics(self, corps):
        return "BEGIN:VCALENDAR\n" + corps + "\nEND:VCALENDAR\n"

    def test_bihebdo(self):
        texte = self.ics("""BEGIN:VEVENT
UID:garde
SUMMARY:Garde
DTSTART;VALUE=DATE:20260904
DTEND;VALUE=DATE:20260911
RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL=20261231
END:VEVENT""")
        lignes = ts.evenements_agenda("Privé", texte, self.AUJOURDHUI)
        debuts = sorted(l["debut"][:10] for l in lignes)
        self.assertGreater(len(lignes), 4, "la garde revient toutes les deux semaines")
        self.assertIn("2026-10-02", debuts)
        self.assertIn("2026-10-16", debuts)
        self.assertNotIn("2026-10-09", debuts, "une semaine sur deux seulement")
        self.assertEqual(len(set(l["timetree_uid"] for l in lignes)), len(lignes), "un identifiant par occurrence")
        self.assertTrue(all(l["timetree_uid"].startswith("garde#") for l in lignes))

    def test_duree_conservee(self):
        texte = self.ics("""BEGIN:VEVENT
UID:garde
SUMMARY:Garde
DTSTART;VALUE=DATE:20260904
DTEND;VALUE=DATE:20260911
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3
END:VEVENT""")
        for l in ts.evenements_agenda("Privé", texte, self.AUJOURDHUI):
            jours = (datetime.fromisoformat(l["fin"]) - datetime.fromisoformat(l["debut"])).days
            self.assertEqual(jours, 7, "une semaine de garde reste une semaine")

    def test_exdate_exclue(self):
        texte = self.ics("""BEGIN:VEVENT
UID:judo
SUMMARY:Judo
DTSTART;TZID=Europe/Paris:20261007T163000
DTEND;TZID=Europe/Paris:20261007T181500
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/Paris:20261014T163000
END:VEVENT""")
        lignes = ts.evenements_agenda("Privé", texte, self.AUJOURDHUI)
        jours = sorted(l["debut"][:10] for l in lignes)
        self.assertEqual(jours, ["2026-10-07", "2026-10-21", "2026-10-28"])
        self.assertFalse(lignes[0]["journee"])
        self.assertIn("T16:30:00", lignes[0]["debut"])

    def test_sans_rrule_une_seule_ligne(self):
        texte = self.ics("""BEGIN:VEVENT
UID:simple
SUMMARY:RDV
DTSTART;VALUE=DATE:20261005
END:VEVENT""")
        lignes = ts.evenements_agenda("Privé", texte, self.AUJOURDHUI)
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["timetree_uid"], "simple", "pas d'identifiant dérivé sans récurrence")

    def test_plafond(self):
        texte = self.ics("""BEGIN:VEVENT
UID:quotidien
SUMMARY:Rappel
DTSTART;TZID=Europe/Paris:20260101T080000
RRULE:FREQ=DAILY
END:VEVENT""")
        lignes = ts.evenements_agenda("Privé", texte, self.AUJOURDHUI)
        self.assertLessEqual(len(lignes), ts.RECURRENCE_MAX, "un quotidien sans fin ne remplit pas la base")
        self.assertGreater(len(lignes), 100)

    def test_recurrent_ancien_compte_quand_meme(self):
        texte = self.ics("""BEGIN:VEVENT
UID:vieux
SUMMARY:Entraînement
DTSTART;TZID=Europe/Paris:20240110T180000
DTEND;TZID=Europe/Paris:20240110T193000
RRULE:FREQ=WEEKLY
END:VEVENT""")
        lignes = ts.evenements_agenda("Privé", texte, self.AUJOURDHUI)
        self.assertTrue(lignes, "un récurrent commencé en 2024 a des occurrences aujourd'hui")
        self.assertTrue(all(l["debut"][:10] >= "2026-07-03" for l in lignes))


class Corps(unittest.TestCase):
    def test_journee_entiere_minuit_utc(self):
        corps = te.contenu_agenda({"titre": "Dépôt écran", "debut": "2026-10-13T00:00:00+00:00",
                                   "fin": "2026-10-14T00:00:00+00:00", "journee": True, "lieu": "Le Lion"})
        self.assertTrue(corps["all_day"])
        self.assertEqual(corps["start_timezone"], "UTC")
        self.assertEqual(corps["start_at"], te.minuit_utc_ms("2026-10-13"))
        self.assertEqual(corps["end_at"], te.minuit_utc_ms("2026-10-14"))
        self.assertEqual(corps["location"], "Le Lion")
        for interdit in ("category", "attendees", "alerts", "recurrences", "file_uuids"):
            self.assertNotIn(interdit, corps, "creer() les ajoute déjà")

    def test_horaire_epoch_paris(self):
        corps = te.contenu_agenda({"titre": "RDV", "debut": "2026-10-13T17:00:00+02:00", "journee": False})
        self.assertFalse(corps["all_day"])
        self.assertEqual(corps["start_timezone"], "Europe/Paris")
        attendu = int(datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(corps["start_at"], attendu)
        self.assertEqual(corps["end_at"], attendu + 3600 * 1000, "sans fin : une heure")


class Ecriture(unittest.TestCase):
    def lancer(self, a_creer, a_retirer, calendriers):
        faux = FausseRegie()
        tt = FauxTimeTree(calendriers)
        ancien = te.ts.requete
        te.ts.requete = faux.requete
        try:
            te.agenda_pousser(tt, {}, a_creer, a_retirer)
        finally:
            te.ts.requete = ancien
        return faux, tt

    def test_creation(self):
        ligne = {"id": 7, "calendrier": "Privé", "titre": "État des lieux", "journee": True,
                 "debut": "2026-10-01T00:00:00+00:00", "fin": None, "lieu": None}
        faux, tt = self.lancer([ligne], [], {"Privé": "cal-prive"})
        self.assertEqual(len(tt.crees), 1)
        self.assertEqual(tt.crees[0][0], "cal-prive")
        self.assertEqual(tt.crees[0][1]["title"], "État des lieux")
        patch = [a for a in faux.appels if a[0] == "PATCH"][0]
        self.assertIn("agenda_events?id=eq.7", patch[1])
        self.assertEqual(patch[2], {"timetree_uid": "uuid-1", "a_ecrire": False})

    def test_calendrier_introuvable_note_l_erreur(self):
        ligne = {"id": 8, "calendrier": "Inconnu", "titre": "X", "journee": True, "debut": "2026-10-01T00:00:00+00:00"}
        faux, tt = self.lancer([ligne], [], {"Privé": "cal-prive"})
        self.assertEqual(tt.crees, [])
        patch = [a for a in faux.appels if a[0] == "PATCH"][0]
        self.assertFalse(patch[2]["a_ecrire"], "on ne boucle pas sur une ligne fautive")
        self.assertIn("erreur", patch[2]["categorie"])

    def test_suppression(self):
        ligne = {"id": 9, "calendrier": "Privé", "timetree_uid": "u-42", "titre": "X",
                 "journee": True, "debut": "2026-10-01T00:00:00+00:00"}
        faux, tt = self.lancer([], [ligne], {"Privé": "cal-prive"})
        self.assertEqual(tt.supprimes, [("cal-prive", "u-42")])
        self.assertIn(("DELETE", "agenda_events?id=eq.9", None), faux.appels)

    def test_files_lues(self):
        faux = FausseRegie({"source=eq.planif": [{"id": 1}], "a_supprimer=eq.true": [{"id": 2}]})
        ancien = te.ts.requete
        te.ts.requete = faux.requete
        try:
            a_creer, a_retirer = te.agenda_a_faire({})
        finally:
            te.ts.requete = ancien
        self.assertEqual(a_creer, [{"id": 1}])
        self.assertEqual(a_retirer, [{"id": 2}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
