#!/usr/bin/env python3
# La Régie — supprimer des événements PRÉCIS d'un agenda TimeTree (par uuid).
#
# Usage visé : retirer d'un agenda des événements qu'on a copiés ailleurs
# (ex. les concours recopiés de Privé vers Work, à enlever de Privé).
#
# À BLANC par défaut : vérifie que les uuid existent dans l'agenda et compte.
# CONFIRM=1 pour supprimer réellement. Irréversible côté TimeTree.
# Dépôt PUBLIC : journaux = nombres uniquement, aucun code ni titre.
#
# Secrets : TIMETREE_EMAIL, TIMETREE_PASSWORD,
#   TIMETREE_CAL  (nom ou code de l'agenda ; défaut « Privé »),
#   TIMETREE_UIDS (uuid séparés par des virgules),
#   CONFIRM=1 pour supprimer.
import os, re, sys, json, unicodedata
import requests
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar

API = "https://timetreeapp.com/api/v1"


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def resoudre(actifs, cle):
    cle = (cle or "").strip()
    if not cle:
        return None
    u = re.search(r"timetreeapp\.com/\w+/([A-Za-z0-9_-]+)", cle)
    if u:
        cle = u.group(1)
    code = cle[1:] if cle.startswith("#") else cle
    for m in actifs:
        if str(m.get("id")) == code or (m.get("alias_code") or "") == code:
            return m
    for m in actifs:
        if norm(m.get("name")) == norm(cle):
            return m
    return None


def main():
    confirmer = os.environ.get("CONFIRM", "").strip() in ("1", "true", "oui", "yes")
    cal_cle = os.environ.get("TIMETREE_CAL") or "Privé"
    uids = [u for u in re.split(r"[,\s]+", os.environ.get("TIMETREE_UIDS") or "") if u]
    if not uids:
        print("TIMETREE_UIDS manquant (aucun événement à supprimer).")
        return 1

    session_id = login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"])
    if not session_id:
        print("Connexion TimeTree refusée.")
        return 1
    reader = TimeTreeCalendar(session_id)
    actifs = [m for m in reader.get_metadata() if m.get("deactivated_at") is None]
    cal = resoudre(actifs, cal_cle)
    if not cal:
        print("Agenda introuvable.")
        return 1

    presents = {e.get("uuid") for e in reader.get_events(cal["id"], cal.get("name"))}
    trouves = [u for u in uids if u in presents]
    absents = [u for u in uids if u not in presents]
    print("À supprimer : %d demandé(s), %d présent(s) dans l'agenda, %d déjà absent(s)."
          % (len(uids), len(trouves), len(absents)))

    if not confirmer:
        print("SIMULATION (CONFIRM non posé) : rien n'a été supprimé.")
        return 0

    # Session d'écriture avec jeton CSRF.
    s = requests.Session()
    s.cookies.set("_session_id", session_id, domain="timetreeapp.com")
    page = s.get("https://timetreeapp.com/", timeout=20).text
    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page, re.I)
    if not m:
        print("Jeton CSRF introuvable.")
        return 1
    entetes = {"Content-Type": "application/json", "X-Timetreea": "web/2.1.0/en", "x-csrf-token": m.group(1)}

    ok = err = 0
    for u in trouves:
        r = s.delete("%s/calendar/%s/event/%s" % (API, cal["id"], u), headers=entetes, timeout=20)
        if r.status_code < 300 or r.status_code == 404:
            ok += 1
        else:
            err += 1
    print("Suppression terminée : %d retiré(s), %d échec(s)." % (ok, err))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
