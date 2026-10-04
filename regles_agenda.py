#!/usr/bin/env python3
# La Régie — événements qui découlent d'autres événements.
#
# Le judo : le mercredi 16 h 30 – 18 h 15, seulement les semaines où Julien a la garde
# (bloc « 👨‍👦‍👦 » du calendrier Privé), hors vacances scolaires zone B et jours fériés,
# jusqu'à fin juin 2027.
#
# La garde se change dans TimeTree. Ce script tourne APRÈS la synchro (timetree_sync.py),
# relit la garde fraîche dans agenda_events et compare semaine par semaine :
#   · semaine de garde sans judo        → une ligne a_ecrire (timetree_ecrire.py la pose)
#   · judo dans une semaine sans garde  → a_supprimer (timetree_ecrire.py le retire)
# La comparaison se fait à la SEMAINE, pas au jour : un judo déplacé à la main au jeudi
# de la même semaine reste le judo de cette semaine, il n'est ni doublé ni retiré.
#
# Un judo supprimé depuis La Régie devient « judo_annule » : la semaine est alors exclue,
# il ne revient pas au passage suivant (voir agenda_pousser dans timetree_ecrire.py).
#
# Sans le calendrier scolaire officiel (réseau), on ne fait RIEN : mieux vaut un judo
# en retard d'un passage qu'un judo posé en pleines vacances.
# Journal public (dépôt public) : que des nombres, aucune date ni titre.
import sys, json, urllib.parse, urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import timetree_sync as ts

PARIS = ZoneInfo("Europe/Paris")
CALENDRIER = "Privé"
MARQUE_GARDE = "👨‍👦‍👦"
JUDO = {"titre": "🥋 Judo", "jour": 2, "debut": time(16, 30), "fin": time(18, 15),
        "jusqu_au": date(2027, 6, 30), "zone": "Zone B"}
CAL_SCOLAIRE = ("https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/"
                "fr-en-calendrier-scolaire/records")


def semaine(d):
    iso = d.isocalendar()
    return "%d-%02d" % (iso[0], iso[1])


def jour_paris(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PARIS).date()


def feries(annee):
    """Les fériés fixes. Ceux qui dépendent de Pâques tombent un lundi ou un jeudi :
    jamais un mercredi, inutile de les calculer ici."""
    return {date(annee, m, j) for m, j in ((1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25))}


def vacances(zone, debut, fin):
    """Les périodes de vacances de la zone qui touchent [debut, fin], en dates de Paris.
    L'API donne le début la veille au soir en UTC (« 2026-10-16T22:00 » = samedi 17)."""
    annees = {"%d-%d" % (a, a + 1) for a in range(debut.year - 1, fin.year + 1)}
    filtre = 'zones="%s" and (%s)' % (zone, " or ".join('annee_scolaire="%s"' % a for a in sorted(annees)))
    url = CAL_SCOLAIRE + "?" + urllib.parse.urlencode({"where": filtre, "select": "start_date,end_date", "limit": 100})
    with urllib.request.urlopen(url, timeout=20) as r:
        lignes = json.load(r).get("results") or []
    if not lignes:
        raise RuntimeError("calendrier scolaire vide")
    # end_date = jour de la rentrée (même convention UTC) : les vacances s'arrêtent la veille.
    return {(jour_paris(l["start_date"]), jour_paris(l["end_date"]) - timedelta(days=1)) for l in lignes}


def jours_couverts(row):
    """Les jours (Paris) couverts par un événement. Fin exclusive pour une journée entière."""
    d0 = jour_paris(row["debut"]) if not row.get("journee") else date.fromisoformat(row["debut"][:10])
    if not row.get("fin"):
        return {d0}
    d1 = date.fromisoformat(row["fin"][:10]) - timedelta(days=1) if row.get("journee") else jour_paris(row["fin"])
    return {d0 + timedelta(days=i) for i in range(max((d1 - d0).days, 0) + 1)}


def judos_voulus(gardes, conges, aujourdhui, regle=JUDO):
    """{semaine: date du mercredi} des judos à avoir, à partir d'aujourd'hui."""
    couverts = set()
    for g in gardes:
        couverts |= jours_couverts(g)
    voulus = {}
    for d in sorted(couverts):
        if d < aujourdhui or d > regle["jusqu_au"] or d.weekday() != regle["jour"]:
            continue
        if d in feries(d.year) or any(a <= d <= b for a, b in conges):
            continue
        voulus[semaine(d)] = d
    return voulus


def ligne_judo(d, regle=JUDO):
    return {"calendrier": CALENDRIER, "titre": regle["titre"], "journee": False,
            "debut": datetime.combine(d, regle["debut"], PARIS).isoformat(),
            "fin": datetime.combine(d, regle["fin"], PARIS).isoformat(),
            "categorie": "judo", "source": "regle", "a_ecrire": True}


def plan(gardes, judos, annules, conges, aujourdhui):
    """Ce qu'il faut créer et retirer. Fonction pure : c'est elle que testent les tests."""
    voulus = judos_voulus(gardes, conges, aujourdhui)
    exclues = {semaine(jour_paris(a["debut"])) for a in annules}
    presents = {}
    for j in judos:
        presents.setdefault(semaine(jour_paris(j["debut"])), []).append(j)
    a_creer = [ligne_judo(d) for s, d in sorted(voulus.items()) if s not in presents and s not in exclues]
    a_retirer = [j for s, liste in presents.items() if s not in voulus for j in liste]
    # deux judos dans la même semaine (ne devrait pas arriver) : on garde le premier
    a_retirer += [j for s, liste in presents.items() if s in voulus for j in sorted(liste, key=lambda x: x["id"])[1:]]
    return a_creer, a_retirer


def main():
    env = ts.charger_env()
    if not env.get("SUPABASE_URL") or not env.get("SUPABASE_SERVICE_KEY"):
        print("SUPABASE absent : pas de règle d'agenda.")
        return 0
    aujourdhui = datetime.now(PARIS).date()
    try:
        conges = vacances(JUDO["zone"], aujourdhui, JUDO["jusqu_au"])
    except Exception as err:
        print("Judo : calendrier scolaire injoignable (%s), rien changé." % str(err)[:80])
        return 0
    cal = urllib.parse.quote(CALENDRIER, safe="")
    depuis = urllib.parse.quote((aujourdhui - timedelta(days=7)).isoformat(), safe="")
    champs = "select=id,titre,debut,fin,journee,categorie,timetree_uid,a_supprimer"
    lignes = ts.tout_lire(env, "agenda_events?calendrier=eq.%s&fin=gte.%s&%s" % (cal, depuis, champs))
    gardes = [l for l in lignes if MARQUE_GARDE in (l.get("titre") or "") and not l.get("a_supprimer")]
    if not gardes:
        # Pas de garde lue (export raté, calendrier renommé) : surtout ne rien retirer.
        print("Judo : aucune semaine de garde lue, rien changé.")
        return 0
    judos = [l for l in lignes if l.get("categorie") == "judo" and not l.get("a_supprimer")
             and jour_paris(l["debut"]) >= aujourdhui]
    annules = [l for l in lignes if l.get("categorie") == "judo_annule"]
    a_creer, a_retirer = plan(gardes, judos, annules, conges, aujourdhui)
    if a_creer:
        ts.requete(env, "POST", "agenda_events", a_creer, prefer="return=minimal")
    for j in a_retirer:
        if j.get("timetree_uid"):
            ts.requete(env, "PATCH", "agenda_events?id=eq.%s" % j["id"],
                       {"a_supprimer": True, "categorie": "judo_retire"}, prefer="return=minimal")
        else:
            ts.requete(env, "DELETE", "agenda_events?id=eq.%s" % j["id"], prefer="return=minimal")
    print("Judo : %d à poser, %d à retirer (%d semaines de garde lues)." % (len(a_creer), len(a_retirer), len(gardes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
