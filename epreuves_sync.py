#!/usr/bin/env python3
# La Régie — l'ordre des épreuves, lu dans Concours_DCK.csv.
#
# Ce fichier vit dans la Dropbox de Julien et alimente déjà le pipeline vidéo
# (Num Concours ; Numero Epreuve ; Nom Epreuve ; Ville ; Nom du concours ;
# Organisateur ; Date de debut ; Date de fin). La Régie tourne en ligne et ne
# voit pas la Dropbox du Mac : Julien crée un LIEN DE PARTAGE vers ce fichier,
# posé dans le secret DROPBOX_CONCOURS_CSV. On le lit, on range les épreuves,
# et on relie chaque numéro de concours au concours de La Régie qui tombe aux
# mêmes dates et au même endroit.
#
# Rien n'est jamais supprimé : le fichier ne contient que les concours du
# moment, les précédents restent en base. Journal public : que des nombres.
import csv, io, os, sys, urllib.request, urllib.error
from datetime import date, datetime, timezone

import timetree_sync as ts


def lien_direct(lien):
    """Un lien de partage Dropbox s'ouvre dans une page ; dl=1 donne le fichier brut."""
    lien = lien.strip()
    if "dl=0" in lien:
        return lien.replace("dl=0", "dl=1")
    if "dl=1" in lien or "raw=1" in lien:
        return lien
    return lien + ("&" if "?" in lien else "?") + "dl=1"


def main():
    lien = os.environ.get("DROPBOX_CONCOURS_CSV", "").strip()
    if not lien:
        print("Lien Dropbox des épreuves absent : rien à lire.")
        return 0
    env = ts.charger_env()
    try:
        req = urllib.request.Request(lien_direct(lien), headers={"User-Agent": "regie-collecteur"})
        with urllib.request.urlopen(req, timeout=30) as r:
            brut = r.read().decode("utf-8-sig")
    except Exception as e:
        print("Concours_DCK.csv illisible : %s" % type(e).__name__)
        return 0

    lignes = list(csv.DictReader(io.StringIO(brut), delimiter=";"))
    epreuves, concours = [], {}
    maintenant = datetime.now(timezone.utc).isoformat()
    for rang, l in enumerate(lignes):
        num = (l.get("Num Concours") or "").strip()
        n = (l.get("Numero Epreuve") or "").strip()
        if not num or not n.isdigit():
            continue
        e = {"num_concours": num, "numero": int(n), "ordre": rang, "nom": (l.get("Nom Epreuve") or "").strip() or "Épreuve %s" % n,
             "ville": (l.get("Ville du concours") or "").strip() or None,
             "nom_concours": (l.get("Nom du concours") or "").strip() or None,
             "organisateur": (l.get("Organisateur") or "").strip() or None,
             "date_debut": (l.get("Date de debut") or "").strip() or None,
             "date_fin": (l.get("Date de fin") or "").strip() or None, "vu_le": maintenant}
        epreuves.append(e)
        concours.setdefault(num, e)
    if epreuves:
        ts.requete(env, "POST", "epreuves?on_conflict=num_concours,numero", epreuves,
                   prefer="resolution=merge-duplicates,return=minimal")

    # Relier chaque numéro au concours de La Régie : mêmes dates, même endroit.
    clients = {c["id"]: c["nom"] for c in ts.tout_lire(env, "clients?select=id,nom")}
    evenements = ts.tout_lire(env, "evenements?select=id,titre,titre_agenda,lieu,client_id,date_debut,date_fin,num_concours"
                                   "&supprime_le=is.null&date_debut=gte.%s" % (date.today().replace(year=date.today().year - 1)).isoformat())
    relies = 0
    for num, e in concours.items():
        if any(ev.get("num_concours") == num for ev in evenements) or not e["date_debut"]:
            continue
        p = {"titre": " ".join(x for x in (e["ville"], e["nom_concours"], e["organisateur"]) if x),
             "debut": date.fromisoformat(e["date_debut"]), "fin": date.fromisoformat(e["date_fin"] or e["date_debut"]), "location": ""}
        trouve = ts.rapprocher(p, [ev for ev in evenements if not ev.get("num_concours")], clients, False)
        if trouve:
            ts.requete(env, "PATCH", "evenements?id=eq.%s" % trouve["id"], {"num_concours": num}, prefer="return=minimal")
            relies += 1
    print("Épreuves : %d lues pour %d concours, %d concours relié(s) à La Régie" % (len(epreuves), len(concours), relies))
    return 0


if __name__ == "__main__":
    sys.exit(main())
