#!/usr/bin/env python3
# La Régie — re-pointer les concours vers un NOUVEL agenda TimeTree.
#
# Contexte : on a recopié l'agenda des concours (ex. « D'clik agency ») vers un
# nouvel agenda (ex. « D'clik Agency - Work »). Les copies ont de NOUVEAUX uuid.
# La Régie, elle, garde l'ancien uuid de chaque concours. Avant de brancher la
# synchro sur le nouvel agenda, il faut remplacer, dans la base, l'ancien uuid
# par celui du jumeau dans le nouvel agenda — sinon la synchro croit que ce sont
# de nouveaux événements (doublons) et retire les anciens.
#
# Appariement ancien -> nouveau : par (titre, start_at, end_at) EXACTS (la copie
# a tout préservé au millième près). Les cas ambigus (même titre + mêmes dates en
# double) sont ignorés et comptés, jamais devinés.
#
# À BLANC par défaut (compte seulement). CONFIRM=1 pour écrire dans la base.
# Dépôt PUBLIC : journaux = nombres uniquement, aucun titre ni uuid.
#
# Secrets : TIMETREE_EMAIL, TIMETREE_PASSWORD,
#   TIMETREE_SRC (ancien agenda ; défaut TIMETREE_CALENDAR_CODE),
#   TIMETREE_DST (nouvel agenda),
#   SUPABASE_URL + SUPABASE_SERVICE_KEY (écriture base, via timetree_sync.charger_env).
import os, re, sys, unicodedata
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar
import timetree_sync as ts


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
    for m in actifs:
        for v in m.values():
            if isinstance(v, (str, int)) and str(v) == cle:
                return m
    return None


def index_par_cle(events):
    """(titre normalisé, start_at, end_at) -> [uuid]. Ignore les occurrences filles."""
    idx = {}
    for e in events:
        if e.get("parent_id") or e.get("deleted_at") or e.get("start_at") is None:
            continue
        k = (norm(e.get("title")), e.get("start_at"), e.get("end_at"))
        idx.setdefault(k, []).append(e.get("uuid"))
    return idx


def main():
    confirmer = os.environ.get("CONFIRM", "").strip() in ("1", "true", "oui", "yes")
    src_cle = os.environ.get("TIMETREE_SRC") or os.environ.get("TIMETREE_CALENDAR_CODE") or ""
    dst_cle = os.environ.get("TIMETREE_DST") or ""
    if not src_cle or not dst_cle:
        print("TIMETREE_SRC (ancien) et TIMETREE_DST (nouvel agenda) sont requis.")
        return 1

    session_id = login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"])
    if not session_id:
        print("Connexion TimeTree refusée.")
        return 1
    reader = TimeTreeCalendar(session_id)
    actifs = [m for m in reader.get_metadata() if m.get("deactivated_at") is None]
    src, dst = resoudre(actifs, src_cle), resoudre(actifs, dst_cle)
    if not src or not dst:
        print("Agenda ancien (%s) ou nouveau (%s) introuvable." % (bool(src), bool(dst)))
        return 1
    if src["id"] == dst["id"]:
        print("Ancien et nouvel agenda identiques : rien à faire.")
        return 1

    anciens = index_par_cle(reader.get_events(src["id"], src.get("name")))
    nouveaux = index_par_cle(reader.get_events(dst["id"], dst.get("name")))

    # ancien uuid -> nouvel uuid, seulement quand l'appariement est NON ambigu des deux côtés.
    old2new = {}
    ambigus = 0
    for k, olds in anciens.items():
        news = nouveaux.get(k, [])
        if len(olds) == 1 and len(news) == 1:
            old2new[olds[0]] = news[0]
        else:
            ambigus += max(len(olds), len(news))
    print("Agenda ancien : %d clés ; nouvel agenda : %d clés ; appariements nets : %d ; ambigus : %d"
          % (len(anciens), len(nouveaux), len(old2new), ambigus))

    env = ts.charger_env()
    evs = ts.tout_lire(env, "evenements?select=id,timetree_uid&source=eq.timetree&timetree_uid=not.is.null")
    a_repointer = [(e["id"], e["timetree_uid"], old2new[e["timetree_uid"]])
                   for e in evs if e.get("timetree_uid") in old2new]
    sans_jumeau = [e for e in evs if e.get("timetree_uid") not in old2new]
    print("Concours La Régie (source=timetree) : %d ; à re-pointer : %d ; sans jumeau net : %d"
          % (len(evs), len(a_repointer), len(sans_jumeau)))

    if not confirmer:
        print("SIMULATION (CONFIRM non posé) : rien n'a été écrit en base.")
        return 0

    ok = err = 0
    for eid, _old, new in a_repointer:
        try:
            ts.requete(env, "PATCH", "evenements?id=eq.%s" % eid,
                       {"timetree_uid": new}, prefer="return=minimal")
            ok += 1
        except Exception:
            err += 1
    print("Re-pointage terminé : %d mis à jour, %d échec(s)." % (ok, err))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
