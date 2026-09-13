#!/usr/bin/env python3
# La Régie — exporter plusieurs calendriers TimeTree en une seule connexion.
#
# Tourne dans GitHub Actions (Python ≥ 3.10, paquet timetree-exporter installé).
# Le calendrier des concours vient de la variable TIMETREE_CALENDRIERS (rôle
# « concours ») ; les calendriers des personnes viennent de la table `personnes`
# de La Régie, que Julien tient à jour lui-même (repli sur la variable si la
# base est injoignable). Format de la variable :
#     Nom du calendrier=rôle;Autre calendrier=rôle
# où le rôle vaut « concours » (le calendrier qui crée les concours) ou le nom
# de la personne qui y figure (« Mya », « Julien »…). Exemple :
#     D'clik agency=concours;Mya=Mya;Elisa=Elisa;Clarys=Clarys;Privé=Julien
#
# Écrit un fichier .ics par calendrier dans le dossier donné, plus un index
# JSON {fichier: rôle}. Le dépôt est PUBLIC et ses journaux aussi : on n'y écrit
# jamais les codes des calendriers, ni aucun titre d'événement.
import json, os, sys, unicodedata

from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar
from timetree_exporter.calendar import Calendar
from timetree_exporter.exporter import Exporter


def normaliser(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def lire_liste(valeur):
    liste = []
    for morceau in (valeur or "").split(";"):
        if "=" in morceau:
            nom, role = morceau.split("=", 1)
            if nom.strip() and role.strip():
                liste.append((nom.strip(), role.strip()))
    return liste


def personnes_de_la_regie():
    """[(calendrier, nom)] des personnes actives qui ont un calendrier, ou None."""
    try:
        import timetree_sync as ts
        env = ts.charger_env()
        if not env.get("SUPABASE_URL"):
            return None
        lignes = ts.requete(env, "GET", "personnes?actif=eq.true&calendrier=not.is.null&select=nom,calendrier") or []
        return [(l["calendrier"], l["nom"]) for l in lignes if (l.get("calendrier") or "").strip()], env
    except Exception:
        return None


def noter_calendrier(env, nom, trouve):
    import timetree_sync as ts
    from datetime import datetime, timezone
    corps = {"calendrier_vu_le": datetime.now(timezone.utc).isoformat(), "calendrier_erreur": None} if trouve \
        else {"calendrier_erreur": "calendrier introuvable dans TimeTree"}
    try:
        ts.requete(env, "PATCH", "personnes?nom=eq.%s" % __import__("urllib.parse").parse.quote(nom), corps, prefer="return=minimal")
    except Exception:
        pass


def noter_etiquettes(env, api, meta, role):
    """Garde dans La Régie les étiquettes de ce calendrier, pour que le cockpit les propose."""
    import timetree_sync as ts
    from datetime import datetime, timezone
    try:
        labels = api.get_labels(meta["id"]) or {}
        couleur = lambda c: ("#" + c.strip().lstrip("#")) if (c or "").strip() else ""
        liste = [{"id": int(k), "nom": v.get("name") or "", "couleur": couleur(v.get("color"))}
                 for k, v in sorted(labels.items(), key=lambda kv: int(kv[0]))
                 if (v.get("name") or "").strip() or (v.get("color") or "").strip()]   # sans nom : par sa couleur
        ts.requete(env, "POST", "calendriers_timetree?on_conflict=nom",
                   [{"nom": meta.get("name"), "role": role, "etiquettes": liste,
                     "vu_le": datetime.now(timezone.utc).isoformat()}],
                   prefer="resolution=merge-duplicates,return=minimal")
    except Exception:
        pass


def main(argv):
    dossier = argv[1] if len(argv) > 1 else "ics"
    variable = lire_liste(os.environ.get("TIMETREE_CALENDRIERS"))
    regie = personnes_de_la_regie()
    env_regie = None
    if regie is not None:
        personnes, env_regie = regie
        voulus = [(n, r) for n, r in variable if r == "concours"] + personnes
    else:
        voulus = variable
    if not voulus:
        code = os.environ.get("TIMETREE_CALENDAR_CODE")
        voulus = [("#" + code, "concours")] if code else []
    if not voulus:
        print("Aucun calendrier demandé.")
        return 1
    os.makedirs(dossier, exist_ok=True)

    api = TimeTreeCalendar(login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"]))
    actifs = [m for m in api.get_metadata() if m.get("deactivated_at") is None]

    index, manquants = {}, []
    for nom, role in voulus:
        if nom.startswith("#"):
            trouves = [m for m in actifs if m.get("alias_code") == nom[1:]]
        else:
            trouves = [m for m in actifs if normaliser(m.get("name")) == normaliser(nom)]
        if not trouves:
            manquants.append(nom if not nom.startswith("#") else "(calendrier par code)")
            if env_regie and role != "concours":
                noter_calendrier(env_regie, role, False)
            continue
        if env_regie and role != "concours":
            noter_calendrier(env_regie, role, True)
        if env_regie:
            noter_etiquettes(env_regie, api, trouves[0], role)
        fichier = "%02d.ics" % (len(index) + 1)
        Exporter(Calendar(api, trouves[0]), os.path.join(dossier, fichier)).export()
        with open(os.path.join(dossier, fichier), "rb") as f:
            n = f.read().count(b"BEGIN:VEVENT")
        index[fichier] = role
        print("Calendrier « %s » exporté · rôle : %s · %d événement(s)" % (
            trouves[0].get("name") if not nom.startswith("#") else "concours", role, n))

    with open(os.path.join(dossier, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    if manquants:
        print("Introuvable(s) dans TimeTree : %s" % ", ".join(manquants))
    return 0 if index else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
