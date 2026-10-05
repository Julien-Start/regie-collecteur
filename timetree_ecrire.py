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
import os, re, sys, json, colorsys, unicodedata
from datetime import datetime, date, timezone, timedelta

import requests
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar

import timetree_sync as ts

API = "https://timetreeapp.com/api/v1"
MAX_TENTATIVES = 3
ORDRE = {"creer": 0, "modifier": 1, "modifier_mission": 1, "copier": 2,
         "copier_mission": 2, "retirer_copie": 3, "retirer_copie_mission": 3,
         "supprimer": 4}


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


def contenu_mission(mi):
    """Comme contenu(), mais pour une MISSION : ce sont SES dates qui font foi.
    Le renfort n'est pas toujours utile toute la durée du concours, et une
    mission peut n'être rattachée à aucun concours. Journée entière, minuit UTC
    du premier et du dernier jour (même convention que les concours)."""
    debut = mi["date_debut"]
    corps = {"title": mi["titre"], "all_day": True,
             "start_at": minuit_utc_ms(debut), "start_timezone": "UTC",
             "end_at": minuit_utc_ms(mi.get("date_fin") or debut), "end_timezone": "UTC"}
    if mi.get("lieu") and normaliser(mi["lieu"]) != normaliser(corps["title"]):
        corps["location"] = mi["lieu"]
    return corps


def iso_to_ms(iso):
    """ISO 8601 (avec ou sans fuseau) -> millisecondes epoch. Sans fuseau : heure de Paris."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            t = t.replace(tzinfo=ZoneInfo("Europe/Paris"))
        except Exception:
            t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp() * 1000)


def contenu_agenda(row):
    """Le corps d'un événement du calendrier unifié : journée entière ou horaire.
    `creer()` ajoute category/attendees/alerts/recurrences/file_uuids : pas ici."""
    corps = {"title": row["titre"]}
    if row.get("lieu"):
        corps["location"] = row["lieu"]
    if row.get("journee", True):
        # journée entière : minuit UTC du premier et du dernier jour, comme contenu().
        # En base, fin = fin EXCLUSIVE (lendemain du dernier jour, comme l'import) ; TimeTree
        # veut le dernier jour INCLUS → on retranche un jour. fin absente = un seul jour.
        d0 = row["debut"][:10]
        if row.get("fin"):
            d1 = (date.fromisoformat(row["fin"][:10]) - timedelta(days=1)).isoformat()
            if d1 < d0:
                d1 = d0
        else:
            d1 = d0
        corps.update(all_day=True, start_at=minuit_utc_ms(d0), start_timezone="UTC",
                     end_at=minuit_utc_ms(d1), end_timezone="UTC")
    else:
        debut = iso_to_ms(row["debut"])
        fin = iso_to_ms(row["fin"]) if row.get("fin") else debut + 3600 * 1000
        corps.update(all_day=False, start_at=debut, start_timezone="Europe/Paris",
                     end_at=fin, end_timezone="Europe/Paris")
    return corps


def agenda_a_faire(env):
    """Les événements à poser, à modifier sur place, et ceux à retirer de TimeTree.
    À poser : planifiés dans La Régie (planif) ou découlant d'une règle (regle, ex. judo).
    À modifier : déjà dans TimeTree (uid connu) et changés dans La Régie (a_ecrire)."""
    a_creer = ts.requete(env, "GET", "agenda_events?source=in.(planif,regle)&a_ecrire=eq.true"
                                     "&timetree_uid=is.null&select=*&order=id") or []
    a_modifier = ts.requete(env, "GET", "agenda_events?a_ecrire=eq.true&a_supprimer=eq.false"
                                        "&timetree_uid=not.is.null&select=*&order=id") or []
    a_retirer = ts.requete(env, "GET", "agenda_events?a_supprimer=eq.true"
                                       "&timetree_uid=not.is.null&select=*&order=id") or []
    return a_creer, a_modifier, a_retirer


def _noter_echec(env, row, champ, err):
    """Une erreur se note dans `categorie`, sauf pour un événement de règle (judo) :
    sa catégorie est son identité, l'écraser ferait reposer un judo à chaque passage.
    Celui-là reste à faire et sera retenté au passage suivant."""
    if (row.get("categorie") or "").startswith("judo"):
        print("Calendrier unifié : échec sur un événement de règle (%s), retenté au prochain passage" % str(err)[:80])
        return
    ts.requete(env, "PATCH", "agenda_events?id=eq.%s" % row["id"],
               {champ: False, "categorie": ("erreur : " + str(err))[:120]}, prefer="return=minimal")


def agenda_pousser(tt, env, a_creer, a_retirer, a_modifier=()):
    """Pose les événements planifiés dans le calendrier voulu, modifie sur place ceux qui
    ont changé (même uid, l'événement reste le même dans TimeTree), retire ceux à supprimer.
    Une erreur sur une ligne n'arrête pas les autres."""
    faits = echecs = 0
    for row in a_creer:
        try:
            uuid = tt.creer(tt.calendrier(row["calendrier"]), contenu_agenda(row))
            ts.requete(env, "PATCH", "agenda_events?id=eq.%s" % row["id"],
                       {"timetree_uid": uuid, "a_ecrire": False}, prefer="return=minimal")
            faits += 1
        except Exception as err:
            _noter_echec(env, row, "a_ecrire", err)
            echecs += 1
    for row in a_modifier:
        try:
            if "#" in row["timetree_uid"]:
                # occurrence dépliée d'une série : pas d'uuid TimeTree à elle seule
                raise RuntimeError("occurrence d'une série, à modifier dans TimeTree")
            tt.modifier(tt.calendrier(row["calendrier"]), row["timetree_uid"], contenu_agenda(row))
            ts.requete(env, "PATCH", "agenda_events?id=eq.%s" % row["id"],
                       {"a_ecrire": False}, prefer="return=minimal")
            faits += 1
        except Exception as err:
            _noter_echec(env, row, "a_ecrire", err)
            echecs += 1
    for row in a_retirer:
        try:
            tt.supprimer(tt.calendrier(row["calendrier"]), row["timetree_uid"])
            if row.get("categorie") == "judo":
                # Supprimé par Julien : on garde la trace pour que la règle ne le repose pas.
                ts.requete(env, "PATCH", "agenda_events?id=eq.%s" % row["id"],
                           {"categorie": "judo_annule", "timetree_uid": None, "a_supprimer": False,
                            "source": "regle"}, prefer="return=minimal")
            else:
                ts.requete(env, "DELETE", "agenda_events?id=eq.%s" % row["id"], prefer="return=minimal")
            faits += 1
        except Exception as err:
            _noter_echec(env, row, "a_supprimer", err)
            echecs += 1
    if faits or echecs:
        print("Calendrier unifié : %d événement(s) posé(s), modifié(s) ou retiré(s), %d en échec" % (faits, echecs))
    return faits, echecs


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
        self._labels = {}

    def calendrier(self, nom):
        cal = self.calendriers.get(normaliser(nom))
        if not cal:
            raise RuntimeError("calendrier introuvable dans TimeTree")
        return cal["id"]

    def etiquette(self, cal_id, nom):
        """L'identifiant de l'étiquette qui porte ce nom dans ce calendrier, ou None."""
        if not nom:
            return None
        if re.fullmatch(r"#\d+", nom.strip()):      # étiquette sans nom, désignée par son numéro
            return int(nom.strip()[1:])
        if cal_id not in self._labels:
            self._labels[cal_id] = self.lecture.get_labels(cal_id) or {}
        return next((int(k) for k, v in self._labels[cal_id].items() if normaliser(v.get("name")) == normaliser(nom)), None)

    def etiquettes_couleur(self, cal_id):
        """Julien veut la SHF en BLEU et les autres clients en ROUGE. On ne connaît pas les
        noms de ses étiquettes, mais leurs couleurs : on prend la plus proche de chaque teinte."""
        labels = self.lecture.get_labels(cal_id) or {}
        palette = []
        for k, v in labels.items():
            hexa = (v.get("color") or "").strip().lstrip("#")
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", hexa):
                continue
            r, g, b = (int(hexa[i:i + 2], 16) / 255 for i in (0, 2, 4))
            h, l, sat = colorsys.rgb_to_hls(r, g, b)
            palette.append((int(k), hexa.upper(), h * 360, sat))
        print("Étiquettes du calendrier des concours : " + ", ".join("%d=#%s" % (i, x) for i, x, _, _ in sorted(palette)))

        def plus_proche(cible):
            vives = [p for p in palette if p[3] >= 0.25]
            if not vives:
                return None
            return min(vives, key=lambda p: min(abs(p[2] - cible), 360 - abs(p[2] - cible)))[0]
        bleu, rouge = plus_proche(215), plus_proche(0)
        print("Bleu (SHF) → étiquette %s · rouge (autres) → étiquette %s" % (bleu, rouge))
        return bleu, rouge

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
                                     "&select=id,evenement_id,mission_id,action,personne,tentatives"
                                     "&order=id") or []
    # Le calendrier unifié : événements planifiés dans La Régie, à poser ou à retirer.
    try:
        agenda_creer, agenda_modifier, agenda_retirer = agenda_a_faire(env)
    except Exception as err:
        agenda_creer, agenda_modifier, agenda_retirer = [], [], []
        print("Calendrier unifié illisible : %s" % str(err)[:200])
    if not attente and not agenda_creer and not agenda_modifier and not agenda_retirer:
        print("Rien à écrire dans TimeTree.")
        return 0

    tt = TimeTree()
    if agenda_creer or agenda_modifier or agenda_retirer:
        agenda_pousser(tt, env, agenda_creer, agenda_retirer, agenda_modifier)
    if not attente:
        return 0
    nom_concours = nom_calendrier_concours()
    if not nom_concours:
        print("Aucun calendrier « concours » déclaré : les concours ne sont pas écrits.")
        return 0
    cal_concours = tt.calendrier(nom_concours)
    # Étiquettes de « D'clik agency » par leur NOM (SHF, DCK) ; repli sur la couleur
    # si l'une d'elles a été renommée. A VALIDER n'est jamais écrasée.
    bleu = tt.etiquette(cal_concours, "SHF")
    rouge = tt.etiquette(cal_concours, "DCK")
    if not bleu or not rouge:
        b2, r2 = tt.etiquettes_couleur(cal_concours)
        bleu, rouge = bleu or b2, rouge or r2
    print("Étiquettes : SHF → %s · DCK → %s" % (bleu, rouge))
    ids_shf = {c["id"] for c in ts.tout_lire(env, "clients?select=id,nom") if normaliser(c["nom"]).startswith("shf")}
    pers = ts.tout_lire(env, "personnes?select=nom,calendrier,etiquette")
    calendrier_de = {p["nom"]: p["calendrier"] for p in pers if p.get("calendrier")}
    etiquette_de = {p["nom"]: p.get("etiquette") for p in pers}

    attente.sort(key=lambda e: (e.get("evenement_id") or 0, e.get("mission_id") or 0,
                                ORDRE.get(e["action"], 9), e["id"]))
    faits = echecs = 0
    for e in attente:
        maintenant = datetime.now(timezone.utc).isoformat()
        eid, action, personne = e.get("evenement_id"), e["action"], e.get("personne")
        mid = e.get("mission_id")
        try:
            # Une MISSION dans l'agenda d'un prestataire : ses dates à elle font
            # foi (0073). Chemin séparé de celui des concours, pour que la boucle
            # qui fait suivre les copies d'un concours ne vienne jamais réécrire
            # ces dates-là.
            if mid:
                mi = (ts.requete(env, "GET",
                      "missions?id=eq.%s&select=titre,lieu,date_debut,date_fin" % mid) or [None])[0]
                if not mi:
                    raise RuntimeError("mission supprimée de La Régie")
                copies_m = ts.requete(env, "GET", "copies_mission_timetree?mission_id=eq.%s"
                                      "&select=id,personne,calendrier,uuid" % mid) or []
                corps_m = contenu_mission(mi)

                if action == "copier_mission":
                    if not any(c["personne"] == personne for c in copies_m):
                        nom_cal = calendrier_de.get(personne)
                        if not nom_cal:
                            raise RuntimeError("cette personne n'a pas de calendrier TimeTree")
                        cal_copie = tt.calendrier(nom_cal)
                        lab = tt.etiquette(cal_copie, etiquette_de.get(personne))
                        uuid = tt.creer(cal_copie, dict(corps_m, label_id=lab) if lab else corps_m)
                        ts.requete(env, "POST",
                                   "copies_mission_timetree?on_conflict=mission_id,personne",
                                   [{"mission_id": mid, "personne": personne,
                                     "calendrier": nom_cal, "uuid": uuid}],
                                   prefer="resolution=merge-duplicates,return=minimal")

                elif action == "modifier_mission":
                    # La mission a bougé (dates, horaires, lieu) : la copie suit.
                    for c in copies_m:
                        if c["personne"] == personne:
                            cal_copie = tt.calendrier(c["calendrier"])
                            lab = tt.etiquette(cal_copie, etiquette_de.get(personne))
                            tt.modifier(cal_copie, c["uuid"],
                                        dict(corps_m, label_id=lab) if lab else corps_m)

                elif action == "retirer_copie_mission":
                    for c in copies_m:
                        if c["personne"] == personne:
                            tt.supprimer(tt.calendrier(c["calendrier"]), c["uuid"])
                            ts.requete(env, "DELETE", "copies_mission_timetree?id=eq.%s" % c["id"],
                                       prefer="return=minimal")

                ts.requete(env, "PATCH", "ecritures_timetree?id=eq.%s" % e["id"],
                           {"statut": "envoye", "message": None, "traite_le": maintenant,
                            "tentatives": (e.get("tentatives") or 0) + 1}, prefer="return=minimal")
                faits += 1
                continue

            ev = (ts.requete(env, "GET", "evenements?id=eq.%s&select=*" % eid) or [None])[0]
            if not ev:
                raise RuntimeError("concours supprimé de La Régie")
            copies = ts.requete(env, "GET", "copies_timetree?evenement_id=eq.%s&select=id,personne,calendrier,uuid" % eid) or []
            corps = contenu(ev)
            efface = False

            if action in ("creer", "modifier"):
                principal = dict(corps)
                if ev.get("etiquette_choisie"):          # étiquette choisie par Julien : elle gagne
                    lab = tt.etiquette(cal_concours, ev["etiquette_choisie"])
                    if lab:
                        principal["label_id"] = lab
                else:
                    a_valider = normaliser(ev.get("etiquette_agenda")) == "a valider"
                    couleur = bleu if ev.get("client_id") in ids_shf else rouge
                    if couleur and not a_valider:
                        principal["label_id"] = couleur
                # Un concours inscrit dans l'agenda d'une personne n'existe QUE par sa copie.
                if ev.get("source") in ("timetree", "regie") and not ev.get("calendrier"):
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
                    cal_copie = tt.calendrier(c["calendrier"])
                    choisie = ev.get("etiquette_choisie") if ev.get("calendrier") == c["calendrier"] else None
                    lab = tt.etiquette(cal_copie, choisie or etiquette_de.get(c["personne"]))
                    tt.modifier(cal_copie, c["uuid"], dict(corps, label_id=lab) if lab else corps)

            elif action == "copier":
                if not any(c["personne"] == personne for c in copies):
                    nom_cal = calendrier_de.get(personne)
                    if not nom_cal:
                        raise RuntimeError("cette personne n'a pas de calendrier TimeTree")
                    cal_copie = tt.calendrier(nom_cal)
                    choisie = ev.get("etiquette_choisie") if ev.get("calendrier") == nom_cal else None
                    lab = tt.etiquette(cal_copie, choisie or etiquette_de.get(personne))
                    uuid = tt.creer(cal_copie, dict(corps, label_id=lab) if lab else corps)
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
