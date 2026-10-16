#!/usr/bin/env python3
# La Régie — COPIE PONCTUELLE d'un agenda TimeTree vers un autre.
#
# But : « repartir d'une base ». On lit TOUS les événements d'un agenda SOURCE et
# on les recrée UNE fois dans un agenda DESTINATION déjà créé dans TimeTree.
# Ensuite la destination vit sa vie : il n'y a AUCUN suivi d'UID, donc relancer
# ce script REFERAIT des doublons — c'est voulu, on ne le lance qu'une fois.
#
# Sécurité :
#   * À BLANC par défaut (compte ce qui serait créé). Il faut CONFIRM=1 pour écrire.
#   * Dépôt PUBLIC : les journaux n'impriment que des nombres, jamais un code de
#     calendrier ni un titre d'événement.
#   * TimeTree ne sait pas créer un agenda par l'API : la destination doit déjà
#     exister (créée et ouverte dans l'appli), on n'y écrit que des événements.
#
# Secrets GitHub attendus :
#   TIMETREE_EMAIL, TIMETREE_PASSWORD
#   TIMETREE_SRC  code (alias_code) ou nom de l'agenda source
#                 (défaut : TIMETREE_CALENDAR_CODE = D'clik agency)
#   TIMETREE_DST  code (alias_code) ou nom de l'agenda destination
#   CONFIRM=1     pour écrire réellement (sinon simulation)
import os, re, sys, json, time, colorsys, unicodedata

import requests
from timetree_exporter.api.auth import login
from timetree_exporter.api.calendar import TimeTreeCalendar

API = "https://timetreeapp.com/api/v1"
ENTETE_LECTURE = {"Content-Type": "application/json", "X-Timetreea": "web/2.1.0/en"}


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def resoudre(actifs, cle):
    """Trouve un calendrier par code (alias_code), par URL de partage, ou par nom."""
    cle = (cle or "").strip()
    if not cle:
        return None
    # URL de partage collée -> on garde le dernier segment (le code).
    u = re.search(r"timetreeapp\.com/\w+/([A-Za-z0-9_-]+)", cle)
    if u:
        cle = u.group(1)
    code = cle[1:] if cle.startswith("#") else cle
    for m in actifs:                                   # identifiant interne (id)
        if str(m.get("id")) == code or str(m.get("id")).lower() == code.lower():
            return m
    for m in actifs:                                   # code exact
        if m.get("alias_code") == code:
            return m
    for m in actifs:                                   # code insensible à la casse
        if (m.get("alias_code") or "").lower() == code.lower():
            return m
    for m in actifs:                                   # nom de l'agenda
        if norm(m.get("name")) == norm(cle):
            return m
    for m in actifs:                                   # dernier filet : n'importe quel champ identifiant
        for v in m.values():
            if isinstance(v, (str, int)) and str(v) == cle:
                return m
    return None


def hue_sat(hexa):
    hexa = (hexa or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", hexa):
        return None
    r, g, b = (int(hexa[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    return h * 360, sat


def map_etiquettes(reader, src_id, dst_id):
    """Associe chaque étiquette source à l'étiquette destination de teinte la plus
    proche (les couleurs portent le sens : SHF bleu, DCK rouge…). {src_label_id: dst_label_id}."""
    getl = getattr(reader, "get_labels", None)
    if not getl:
        return {}
    src = getl(src_id) or {}
    dst = getl(dst_id) or {}
    dpal = []
    for k, v in dst.items():
        hs = hue_sat(v.get("color"))
        if hs:
            dpal.append((int(k), hs[0], hs[1]))
    if not dpal:
        return {}
    out = {}
    for k, v in src.items():
        hs = hue_sat(v.get("color"))
        if not hs:
            continue
        vives = [p for p in dpal if p[2] >= 0.25] or dpal
        best = min(vives, key=lambda p: min(abs(p[1] - hs[0]), 360 - abs(p[1] - hs[0])))
        out[int(k)] = best[0]
    return out


class Ecrivain:
    def __init__(self, session_id):
        self.s = requests.Session()
        self.s.cookies.set("_session_id", session_id, domain="timetreeapp.com")
        page = self.s.get("https://timetreeapp.com/", timeout=20).text
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page, re.I)
        if not m:
            raise RuntimeError("jeton CSRF introuvable (TimeTree a peut-être changé)")
        self.entetes = {"Content-Type": "application/json", "X-Timetreea": "web/2.1.0/en",
                        "x-csrf-token": m.group(1)}

    def creer(self, cal_id, corps):
        corps = dict(corps, attendees=[], alerts=[], file_uuids=[])
        corps.setdefault("category", 1)
        corps.setdefault("recurrences", [])
        last = None
        for essai in range(3):
            r = self.s.post("%s/calendar/%s/event" % (API, cal_id), headers=self.entetes,
                            data=json.dumps(corps), timeout=20)
            if r.status_code < 300:
                return (r.json().get("event") or {}).get("uuid")
            last = r.status_code
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (essai + 1))
                continue
            break
        raise RuntimeError("création refusée (HTTP %s)" % last)


def corps_de(ev, lab_map):
    """Reprend l'événement source tel quel (dates, fuseaux, lieu, note, récurrence,
    étiquette re-mappée par couleur). Les timestamps sont déjà en ms côté TimeTree."""
    corps = {
        "title": ev.get("title") or "(sans titre)",
        "all_day": bool(ev.get("all_day")),
        "start_at": ev.get("start_at"),
        "start_timezone": ev.get("start_timezone") or "UTC",
        "end_at": ev.get("end_at") or ev.get("start_at"),
        "end_timezone": ev.get("end_timezone") or "UTC",
        "category": ev.get("category") or 1,
        "type": ev.get("type") or 0,
        "recurrences": ev.get("recurrences") or [],
    }
    if ev.get("location"):
        corps["location"] = ev["location"]
    if ev.get("note"):
        corps["note"] = ev["note"]
    lab = ev.get("label_id")
    if lab is not None and int(lab) in lab_map:
        corps["label_id"] = lab_map[int(lab)]
    return corps


def a_copier(ev):
    """On écarte ce qui n'est pas un vrai événement à recréer."""
    if ev.get("deleted_at") or ev.get("deactivated_at"):
        return False
    if ev.get("parent_id"):            # occurrence fille d'une récurrence : portée par le parent
        return False
    if ev.get("start_at") is None:
        return False
    return True


def main():
    confirmer = os.environ.get("CONFIRM", "").strip() in ("1", "true", "oui", "yes")
    src_cle = os.environ.get("TIMETREE_SRC") or os.environ.get("TIMETREE_CALENDAR_CODE") or ""
    dst_cle = os.environ.get("TIMETREE_DST") or ""
    if not dst_cle:
        print("TIMETREE_DST manquant (code ou nom de l'agenda destination).")
        return 1

    session_id = login(os.environ["TIMETREE_EMAIL"], os.environ["TIMETREE_PASSWORD"])
    if not session_id:
        print("Connexion TimeTree refusée.")
        return 1
    reader = TimeTreeCalendar(session_id)
    actifs = [m for m in reader.get_metadata() if m.get("deactivated_at") is None]

    src = resoudre(actifs, src_cle)
    dst = resoudre(actifs, dst_cle)
    if not src:
        print("Agenda source introuvable.")
        return 1
    if not dst:
        avec_code = sum(1 for m in actifs if m.get("alias_code"))
        brut = (os.environ.get("TIMETREE_DST") or "").strip()
        kind = "URL" if "timetreeapp.com" in brut else ("code/nom")
        print("Agenda destination introuvable.")
        print("Diagnostic : %d agenda(s) actif(s) vus par ce compte (dont %d avec un code) ; "
              "valeur DST reçue : %s de longueur %d." % (len(actifs), avec_code, kind, len(brut)))
        if actifs:
            print("Champs d'identité disponibles par agenda : %s" % ", ".join(sorted(actifs[0].keys())))
        print("Pistes : (1) crée et OUVRE l'agenda dans l'appli avec CE compte TimeTree ; "
              "(2) mets dans TIMETREE_DST soit le NOM exact de l'agenda, soit le code seul "
              "(la partie après le dernier / de l'URL de partage), pas l'URL entière.")
        return 1
    if src["id"] == dst["id"]:
        print("Source et destination sont le même agenda : rien à faire.")
        return 1

    events = reader.get_events(src["id"], src.get("name"))
    a_faire = [e for e in events if a_copier(e)]
    print("Source : %d événement(s) lus, %d à copier." % (len(events), len(a_faire)))

    lab_map = map_etiquettes(reader, src["id"], dst["id"])
    print("Étiquettes re-mappées par couleur : %d." % len(lab_map))

    if not confirmer:
        print("SIMULATION (CONFIRM non posé) : rien n'a été créé. "
              "Relance avec CONFIRM=1 pour écrire les %d événement(s)." % len(a_faire))
        return 0

    ecriv = Ecrivain(session_id)
    ok = err = 0
    for e in a_faire:
        try:
            ecriv.creer(dst["id"], corps_de(e, lab_map))
            ok += 1
        except Exception:
            err += 1
        time.sleep(0.4)      # on ménage l'API non officielle
    print("Copie terminée : %d créé(s), %d échec(s)." % (ok, err))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
