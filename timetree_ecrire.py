#!/usr/bin/env python3
# La Régie — écrire dans TimeTree les concours créés ou modifiés dans La Régie.
#
# TimeTree n'a pas d'API publique. On reprend la technique de TimeTree-MCP
# (github.com/ehs208/TimeTree-MCP, MIT) : session par email et mot de passe,
# jeton CSRF lu dans la page de timetreeapp.com, puis
#   POST /api/v1/calendar/{id}/event           création
#   PUT  /api/v1/calendar/{id}/event/{uuid}    modification
#
# Ne touche QUE le calendrier des concours (rôle « concours » dans
# TIMETREE_CALENDRIERS) et seulement titre, dates et lieu : l'équipe reste
# dans La Régie. Chaque ligne de `ecritures_timetree` envoie l'état COURANT
# de l'événement. Trois échecs et la ligne s'arrête (le cockpit l'affiche).
# Journal public (dépôt public) : aucun titre, aucune date, aucun identifiant.
import os, re, sys, json, unicodedata
from datetime import datetime, date, timezone

import requests
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar

import timetree_sync as ts

API = "https://timetreeapp.com/api/v1"
ENTETES = {"Content-Type": "application/json", "X-Timetreea": "web/2.1.0/en"}
MAX_TENTATIVES = 3


def normaliser(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("’", "'").lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def nom_calendrier_concours():
    for morceau in (os.environ.get("TIMETREE_CALENDRIERS") or "").split(";"):
        if "=" in morceau:
            nom, role = morceau.split("=", 1)
            if role.strip() == "concours":
                return nom.strip()
    return None


def minuit_utc_ms(jour):
    d = date.fromisoformat(jour)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def contenu(ev):
    debut = ev["date_debut"]
    fin = ev.get("date_fin") or debut
    corps = {
        "title": ev["titre"],
        "all_day": True,
        # Journée entière : minuit UTC du premier et du dernier jour (fin incluse).
        "start_at": minuit_utc_ms(debut), "start_timezone": "UTC",
        "end_at": minuit_utc_ms(fin), "end_timezone": "UTC",
    }
    if ev.get("lieu") and normaliser(ev["lieu"]) != normaliser(corps["title"]):
        corps["location"] = ev["lieu"]
    return corps


def main():
    env = ts.charger_env()
    attente = ts.requete(env, "GET", "ecritures_timetree?statut=eq.a_envoyer&select=id,evenement_id,action,tentatives&order=id") or []
    if not attente:
        print("Rien à écrire dans TimeTree.")
        return 0
    nom = nom_calendrier_concours()
    if not nom:
        print("Aucun calendrier « concours » déclaré : rien n'est écrit.")
        return 0

    session_id = login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"])
    lecture = TimeTreeCalendar(session_id)
    cal = next((m for m in lecture.get_metadata()
                if m.get("deactivated_at") is None and normaliser(m.get("name")) == normaliser(nom)), None)
    if not cal:
        print("Calendrier des concours introuvable dans TimeTree : rien n'est écrit.")
        return 1
    labels = lecture.get_labels(cal["id"]) or {}
    label_shf = next((int(k) for k, v in labels.items() if "shf" in normaliser(v.get("name"))), None)

    s = requests.Session()
    s.cookies.set("_session_id", session_id, domain="timetreeapp.com")
    page = s.get("https://timetreeapp.com/", timeout=20).text
    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page, re.I)
    if not m:
        print("Jeton CSRF introuvable : TimeTree a peut-être changé. Rien n'est écrit.")
        return 1
    entetes = dict(ENTETES, **{"x-csrf-token": m.group(1)})

    ids_shf = {c["id"] for c in ts.tout_lire(env, "clients?select=id,nom") if normaliser(c["nom"]).startswith("shf")}
    faits = echecs = 0
    for e in attente:
        maintenant = datetime.now(timezone.utc).isoformat()
        ev = (ts.requete(env, "GET", "evenements?id=eq.%s&select=*" % e["evenement_id"]) or [None])[0]
        if not ev:
            ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                       {"statut": "echec", "message": "événement supprimé", "traite_le": maintenant}, prefer="return=minimal")
            continue
        corps = contenu(ev)
        if label_shf and ev.get("client_id") in ids_shf:
            corps["label_id"] = label_shf
        try:
            if ev.get("timetree_uid"):
                r = s.put("%s/calendar/%s/event/%s" % (API, cal["id"], ev["timetree_uid"]),
                          headers=entetes, data=json.dumps(corps), timeout=20)
            else:
                r = s.post("%s/calendar/%s/event" % (API, cal["id"]),
                           headers=dict(entetes), data=json.dumps(dict(corps, category=1, attendees=[], alerts=[],
                                                                       recurrences=[], file_uuids=[])), timeout=20)
            if r.status_code >= 300:
                raise RuntimeError("TimeTree HTTP %s" % r.status_code)
            if not ev.get("timetree_uid"):
                uuid = (r.json().get("event") or {}).get("uuid")
                if not uuid:
                    raise RuntimeError("TimeTree n'a pas renvoyé d'identifiant")
                # Désormais l'événement EST celui de l'agenda : la synchro le reconnaîtra.
                ts.requete(env, "PATCH", "evenements?id=eq.%s" % ev["id"],
                           {"timetree_uid": uuid, "source": "timetree", "titre_agenda": corps["title"],
                            "cle_agenda": ts.cle_agenda(corps["title"])}, prefer="return=minimal")
            elif ev.get("titre_agenda") != corps["title"]:
                ts.requete(env, "PATCH", "evenements?id=eq.%s" % ev["id"],
                           {"titre_agenda": corps["title"], "cle_agenda": ts.cle_agenda(corps["title"])}, prefer="return=minimal")
            ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                       {"statut": "envoye", "message": None, "traite_le": maintenant,
                        "tentatives": (e.get("tentatives") or 0) + 1}, prefer="return=minimal")
            faits += 1
        except Exception as err:
            n = (e.get("tentatives") or 0) + 1
            ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                       {"statut": "echec" if n >= MAX_TENTATIVES else "a_envoyer", "tentatives": n,
                        "message": str(err)[:300], "traite_le": maintenant}, prefer="return=minimal")
            echecs += 1
    print("TimeTree : %d écriture(s) faite(s), %d en échec" % (faits, echecs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
