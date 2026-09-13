#!/usr/bin/env python3
# La Régie — appliquer dans TimeTree ce que Julien a décidé dans La Régie.
#
# TimeTree n'a pas d'API publique. On reprend la technique de TimeTree-MCP
# (github.com/ehs208/TimeTree-MCP, MIT) : session par email et mot de passe,
# jeton CSRF lu dans la page de timetreeapp.com, puis
#   POST   /api/v1/calendar/{id}/event           créer
#   PUT    /api/v1/calendar/{id}/event/{uuid}    modifier
#   DELETE /api/v1/calendar/{id}/event/{uuid}    supprimer
#
# Actions de la file `ecritures_timetree` :
#   creer · modifier      le concours dans le calendrier des concours (et ses copies)
#   copier (personne)     une copie liée dans le calendrier de la personne
#   retirer_copie         supprime cette copie
#   supprimer             supprime les copies puis le concours, puis la ligne en base
#                         (sauf si un devis ou une facture y est attaché)
# Seuls titre, dates et lieu sont écrits. Chaque écriture envoie l'état COURANT.
# Journal public (dépôt public) : aucun titre, aucune date, aucun identifiant.
import os, re, sys, json, unicodedata
from datetime import datetime, date, timezone

import requests
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar

import timetree_sync as ts

API = "https://timetreeapp.com/api/v1"
MAX_TENTATIVES = 3
ORDRE = {"creer": 0, "modifier": 1, "copier": 2, "retirer_copie": 3, "supprimer": 4}


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
    """Titre, dates et lieu. Journée entière : minuit UTC du premier et du dernier jour
    (vérifié le 13/09/2026 par relecture : TimeTree rend exactement les dates envoyées)."""
    debut = ev["date_debut"]
    corps = {"title": ev["titre"], "all_day": True,
             "start_at": minuit_utc_ms(debut), "start_timezone": "UTC",
             "end_at": minuit_utc_ms(ev.get("date_fin") or debut), "end_timezone": "UTC"}
    if ev.get("lieu") and normaliser(ev["lieu"]) != normaliser(corps["title"]):
        corps["location"] = ev["lieu"]
    return corps


class TimeTree:
    def __init__(self):
        session_id = login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"])
        self.lecture = TimeTreeCalendar(session_id)
        self.calendriers = {normaliser(m.get("name")): m for m in self.lecture.get_metadata()
                            if m.get("deactivated_at") is None}
        self.s = requests.Session()
        self.s.cookies.set("_session_id", session_id, domain="timetreeapp.com")
        page = self.s.get("https://timetreeapp.com/", timeout=20).text
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page, re.I)
        if not m:
            raise RuntimeError("jeton CSRF introuvable (TimeTree a peut-être changé)")
        self.entetes = {"Content-Type": "application/json", "X-Timetreea": "web/2.1.0/en", "x-csrf-token": m.group(1)}

    def calendrier(self, nom):
        cal = self.calendriers.get(normaliser(nom))
        if not cal:
            raise RuntimeError("calendrier introuvable dans TimeTree")
        return cal["id"]

    def label_shf(self, cal_id):
        labels = self.lecture.get_labels(cal_id) or {}
        return next((int(k) for k, v in labels.items() if "shf" in normaliser(v.get("name"))), None)

    def creer(self, cal_id, corps):
        corps = dict(corps, category=1, attendees=[], alerts=[], recurrences=[], file_uuids=[])
        r = self.s.post("%s/calendar/%s/event" % (API, cal_id), headers=self.entetes, data=json.dumps(corps), timeout=20)
        if r.status_code >= 300:
            raise RuntimeError("création refusée par TimeTree (HTTP %s)" % r.status_code)
        uuid = (r.json().get("event") or {}).get("uuid")
        if not uuid:
            raise RuntimeError("TimeTree n'a pas renvoyé d'identifiant")
        return uuid

    def modifier(self, cal_id, uuid, corps):
        r = self.s.put("%s/calendar/%s/event/%s" % (API, cal_id, uuid), headers=self.entetes, data=json.dumps(corps), timeout=20)
        if r.status_code >= 300:
            raise RuntimeError("modification refusée par TimeTree (HTTP %s)" % r.status_code)

    def supprimer(self, cal_id, uuid):
        r = self.s.delete("%s/calendar/%s/event/%s" % (API, cal_id, uuid), headers=self.entetes, timeout=20)
        if r.status_code == 404:
            return                                    # déjà supprimé : c'est le résultat voulu
        if r.status_code >= 300:
            raise RuntimeError("suppression refusée par TimeTree (HTTP %s)" % r.status_code)


def main():
    env = ts.charger_env()
    attente = ts.requete(env, "GET", "ecritures_timetree?statut=eq.a_envoyer"
                                     "&select=id,evenement_id,action,personne,tentatives&order=id") or []
    if not attente:
        print("Rien à écrire dans TimeTree.")
        return 0
    nom_concours = nom_calendrier_concours()
    if not nom_concours:
        print("Aucun calendrier « concours » déclaré : rien n'est écrit.")
        return 0

    tt = TimeTree()
    cal_concours = tt.calendrier(nom_concours)
    label_shf = tt.label_shf(cal_concours)
    ids_shf = {c["id"] for c in ts.tout_lire(env, "clients?select=id,nom") if normaliser(c["nom"]).startswith("shf")}
    calendrier_de = {p["nom"]: p["calendrier"] for p in ts.tout_lire(env, "personnes?select=nom,calendrier") if p.get("calendrier")}

    attente.sort(key=lambda e: (e["evenement_id"], ORDRE.get(e["action"], 9), e["id"]))
    faits = echecs = 0
    for e in attente:
        maintenant = datetime.now(timezone.utc).isoformat()
        eid, action, personne = e["evenement_id"], e["action"], e.get("personne")
        try:
            ev = (ts.requete(env, "GET", "evenements?id=eq.%s&select=*" % eid) or [None])[0]
            if not ev:
                raise RuntimeError("concours supprimé de La Régie")
            copies = ts.requete(env, "GET", "copies_timetree?evenement_id=eq.%s&select=id,personne,calendrier,uuid" % eid) or []
            corps = contenu(ev)
            efface = False

            if action in ("creer", "modifier"):
                principal = dict(corps)
                if label_shf and ev.get("client_id") in ids_shf:
                    principal["label_id"] = label_shf
                if ev.get("source") in ("timetree", "regie"):
                    if ev.get("timetree_uid") and ev.get("source") == "timetree":
                        tt.modifier(cal_concours, ev["timetree_uid"], principal)
                    elif not ev.get("timetree_uid"):
                        uuid = tt.creer(cal_concours, principal)
                        # Désormais l'événement EST celui de l'agenda : la synchro le reconnaîtra.
                        ts.requete(env, "PATCH", "evenements?id=eq.%s" % eid,
                                   {"timetree_uid": uuid, "source": "timetree"}, prefer="return=minimal")
                    ts.requete(env, "PATCH", "evenements?id=eq.%s" % eid,
                               {"titre_agenda": corps["title"], "cle_agenda": ts.cle_agenda(corps["title"])}, prefer="return=minimal")
                for c in copies:                          # les copies suivent le concours
                    tt.modifier(tt.calendrier(c["calendrier"]), c["uuid"], corps)

            elif action == "copier":
                if not any(c["personne"] == personne for c in copies):
                    nom_cal = calendrier_de.get(personne)
                    if not nom_cal:
                        raise RuntimeError("cette personne n'a pas de calendrier TimeTree")
                    uuid = tt.creer(tt.calendrier(nom_cal), corps)
                    ts.requete(env, "POST", "copies_timetree?on_conflict=evenement_id,personne",
                               [{"evenement_id": eid, "personne": personne, "calendrier": nom_cal, "uuid": uuid}],
                               prefer="resolution=merge-duplicates,return=minimal")

            elif action == "retirer_copie":
                for c in copies:
                    if c["personne"] == personne:
                        tt.supprimer(tt.calendrier(c["calendrier"]), c["uuid"])
                        ts.requete(env, "DELETE", "copies_timetree?id=eq.%s" % c["id"], prefer="return=minimal")

            elif action == "supprimer":
                for c in copies:
                    tt.supprimer(tt.calendrier(c["calendrier"]), c["uuid"])
                    ts.requete(env, "DELETE", "copies_timetree?id=eq.%s" % c["id"], prefer="return=minimal")
                if ev.get("timetree_uid") and ev.get("source") == "timetree":
                    tt.supprimer(cal_concours, ev["timetree_uid"])
                    ts.requete(env, "PATCH", "evenements?id=eq.%s" % eid,
                               {"retire_agenda_le": maintenant}, prefer="return=minimal")
                lie = (ts.requete(env, "GET", "devis?evenement_id=eq.%s&select=id&limit=1" % eid) or []) or \
                      (ts.requete(env, "GET", "factures?evenement_id=eq.%s&select=id&limit=1" % eid) or [])
                if not lie:
                    ts.requete(env, "DELETE", "evenements?id=eq.%s" % eid, prefer="return=minimal")
                    efface = True                          # la ligne de file part avec lui

            if not efface:
                ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                           {"statut": "envoye", "message": None, "traite_le": maintenant,
                            "tentatives": (e.get("tentatives") or 0) + 1}, prefer="return=minimal")
            faits += 1
        except Exception as err:
            n = (e.get("tentatives") or 0) + 1
            try:
                ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                           {"statut": "echec" if n >= MAX_TENTATIVES else "a_envoyer", "tentatives": n,
                            "message": str(err)[:300], "traite_le": maintenant}, prefer="return=minimal")
            except Exception:
                pass
            echecs += 1
    print("TimeTree : %d écriture(s) faite(s), %d en échec" % (faits, echecs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
