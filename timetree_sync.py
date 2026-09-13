#!/usr/bin/env python3
# La Régie — la saison des concours, lue dans TimeTree.
#
# TimeTree n'a aucun export officiel. Le workflow timetree.yml lance une fois par
# jour l'outil communautaire timetree-exporter, qui écrit le calendrier « D'clik
# agency » dans un fichier .ics ; ce script le lit et range les concours dans la
# table `evenements`.
#
#   python3 timetree_sync.py dossier/              synchronise un export multi-calendriers
#                                                  (index.json : concours + personnes)
#   python3 timetree_sync.py saison.ics            synchronise le seul calendrier des concours
#   python3 timetree_sync.py saison.ics --essai    montre ce qu'il ferait, n'écrit rien
#   python3 timetree_sync.py --echec "message"     note un échec de l'export (radar)
#
# Règles (voir la migration 0048) :
#   - seules les journées entières sont des concours ; un rendez-vous à 17 h n'en est pas un ;
#   - « SHF … » (ou étiquette SHF) → client SHF, le reste du titre est le lieu ;
#   - sinon le client est DEVINÉ d'après les événements passés au même lieu, SHF exclue,
#     et marqué `client_devine` ; une fois confirmé par Julien, la synchro n'y touche plus ;
#   - LA RÉGIE APPREND : si Julien a corrigé un concours, sa règle (regles_agenda) gagne sur
#     tout le reste, pour toutes les dates et toutes les années ; un événement corrigé à la
#     main (corrige_le) n'est plus jamais modifié sur son lieu ni son client ;
#   - on ne change jamais le statut d'un événement existant ;
#   - un événement qui disparaît de TimeTree n'est jamais supprimé : il est marqué.
# Stdlib uniquement, comme le collecteur.
import sys, os, re, json, ssl, time, hashlib, unicodedata, urllib.request, urllib.parse, urllib.error
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# Les journaux de GitHub Actions d'un dépôt public sont lisibles par tous :
# en CI on n'écrit que des nombres, jamais un titre, un lieu ou un client.
EN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def detail(texte):
    if not EN_CI:
        print(texte)
FENETRE_PASSE = 400    # jours : la saison passée aussi, pour y relier factures et devis
FENETRE_AVENIR = 548   # jours : un an et demi de saison

# Mots d'un titre qui ne désignent pas un lieu (« Lamballe Top 7 », « CSI Montfort »).
MOTS_VIDES = {
    "csi", "cso", "cce", "cir", "shf", "top", "jump", "grand", "prix", "pro", "amateur",
    "club", "tour", "sur", "les", "des", "de", "du", "la", "le", "et", "en", "concours",
    "international", "national", "regional", "finale", "championnat", "championnats",
    "ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii",
    "sur", "les", "des", "aux", "pour", "avec", "une", "est", "jour", "rdv", "am", "pm",
}


# --------------------------------------------------------------------------- #
# Accès Supabase (même idiome que collect.py)                                  #
# --------------------------------------------------------------------------- #

def charger_env():
    env = {}
    for nom in ("cles-supabase.txt", ".env"):
        p = os.path.join(HERE, nom)
        if os.path.exists(p):
            for ligne in open(p, encoding="utf-8"):
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    k, v = ligne.split("=", 1)
                    env[k.strip()] = v.strip()
            break
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def requete(env, methode, chemin, corps=None, prefer=None):
    url = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + chemin
    cle = env["SUPABASE_SERVICE_KEY"]
    entetes = {"apikey": cle, "Authorization": "Bearer " + cle, "Accept": "application/json"}
    donnees = None
    if corps is not None:
        entetes["Content-Type"] = "application/json"
        donnees = json.dumps(corps).encode("utf-8")
    if prefer:
        entetes["Prefer"] = prefer
    # Supabase répond parfois 502/503/504 quelques secondes. On retente, mais
    # jamais une création (POST) : la rejouer pourrait créer un doublon.
    essais = 3 if methode in ("GET", "PATCH", "DELETE") else 1
    for n in range(essais):
        req = urllib.request.Request(url, data=donnees, method=methode, headers=entetes)
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
                brut = r.read().decode("utf-8")
                return json.loads(brut) if brut.strip() else None
        except urllib.error.HTTPError as e:
            if e.code not in (502, 503, 504) or n == essais - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if n == essais - 1:
                raise
        time.sleep(3 * (n + 1))


def tout_lire(env, chemin):
    sortie, depart = [], 0
    while True:
        sep = "&" if "?" in chemin else "?"
        page = requete(env, "GET", chemin + sep + "limit=1000&offset=%d" % depart) or []
        sortie.extend(page)
        if len(page) < 1000:
            return sortie
        depart += 1000


def noter_synchro(env, reussite, message, nb=None):
    maintenant = datetime.now(timezone.utc).isoformat()
    ligne = {"source": "timetree", "dernier_essai": maintenant, "dernier_message": (message or "")[:500]}
    if reussite:
        ligne["derniere_reussite"] = maintenant
        ligne["nb_evenements"] = nb
    requete(env, "POST", "synchros?on_conflict=source", [ligne],
            prefer="resolution=merge-duplicates,return=minimal")


# --------------------------------------------------------------------------- #
# Lecture du .ics                                                              #
# --------------------------------------------------------------------------- #

def _valeur(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\")


def lire_ics(texte):
    """Retourne une liste de dict {NOM: (params, valeur)} par VEVENT."""
    brutes = texte.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lignes = []
    for l in brutes:
        if l[:1] in (" ", "\t") and lignes:
            lignes[-1] += l[1:]          # ligne pliée (RFC 5545)
        else:
            lignes.append(l)
    evenements, courant = [], None
    for l in lignes:
        if l.strip() == "BEGIN:VEVENT":
            courant = {}
        elif l.strip() == "END:VEVENT":
            if courant is not None:
                evenements.append(courant)
            courant = None
        elif courant is not None and ":" in l:
            tete, val = l.split(":", 1)
            morceaux = tete.split(";")
            nom = morceaux[0].upper()
            params = {}
            for p in morceaux[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k.upper()] = v
            if nom not in courant:
                courant[nom] = (params, val)
    return evenements


def _date(prop):
    """(date, journee_entiere) ou (None, False)."""
    if not prop:
        return None, False
    params, val = prop
    val = val.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", val):
        return datetime.strptime(val[:8], "%Y%m%d").date(), True
    return None, False               # heure précise : un rendez-vous, pas un concours


def normaliser(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def concours_de_l_agenda(texte, aujourd_hui):
    retenus, ecartes = [], []
    for ev in lire_ics(texte):
        titre = _valeur(ev.get("SUMMARY", ({}, ""))[1]).strip()
        debut, jour_entier = _date(ev.get("DTSTART"))
        if not titre or not debut or not jour_entier:
            ecartes.append(titre or "(sans titre)")
            continue
        fin_excl, _ = _date(ev.get("DTEND"))
        fin = (fin_excl - timedelta(days=1)) if fin_excl and fin_excl > debut else debut
        if fin < aujourd_hui - timedelta(days=FENETRE_PASSE) or debut > aujourd_hui + timedelta(days=FENETRE_AVENIR):
            continue
        uid = ev.get("UID", ({}, ""))[1].strip() or hashlib.sha1((titre + debut.isoformat()).encode()).hexdigest()
        brutes = [c.strip() for c in _valeur(ev.get("CATEGORIES", ({}, ""))[1]).split(",") if c.strip()]
        cats = [normaliser(c) for c in brutes]
        lieu = _valeur(ev.get("LOCATION", ({}, ""))[1]).strip()
        couleur = ev.get("COLOR", ({}, ""))[1].strip()
        retenus.append({"uid": uid, "titre": titre, "debut": debut, "fin": fin, "categories": cats, "etiquette": brutes[0] if brutes else None, "couleur": couleur, "location": lieu})
    return retenus, ecartes


# --------------------------------------------------------------------------- #
# Rapprochement avec les clients                                               #
# --------------------------------------------------------------------------- #

ROMAINS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}


def cle_agenda(titre):
    """« Lamballe Jump III » et « Lamballe Jump IV » sont le même concours : on retire
    les numéros d'édition et les années. Seule implémentation de la clé (la base la stocke)."""
    mots = [m for m in normaliser(titre).split() if not m.isdigit() and m not in ROMAINS]
    return " ".join(mots) or normaliser(titre)


def est_shf(c):
    return normaliser(c["titre"]).startswith("shf ") or "shf" in c["categories"]


def lieu_de(c):
    if c.get("location"):                        # lieu saisi dans TimeTree : il fait foi
        return c["location"]
    if est_shf(c):
        return re.sub(r"^\s*shf\s*[-:·]?\s*", "", c["titre"], flags=re.I).strip() or c["titre"]
    return c["titre"]


def deviner_client(c, passe, clients, id_shf):
    """Le client le plus souvent associé, dans le passé, au lieu nommé dans le titre (SHF exclue)."""
    if est_shf(c):
        return id_shf, False
    mots = [m for m in normaliser(c["titre"]).split() if len(m) >= 4 and m not in MOTS_VIDES and not m.isdigit()]
    if not mots:
        return None, False
    score, recent = {}, {}
    for e in passe:
        cid = e.get("client_id")
        if not cid or cid == id_shf:
            continue
        botte = normaliser((e.get("lieu") or "") + " " + clients.get(cid, ""))
        if any(m in botte.split() or (len(m) >= 6 and m in botte) for m in mots):
            score[cid] = score.get(cid, 0) + 1
            recent[cid] = max(recent.get(cid, ""), e.get("date_debut") or "")
    if not score:
        return None, False
    meilleur = sorted(score, key=lambda k: (score[k], recent[k]), reverse=True)[0]
    return meilleur, True


# --------------------------------------------------------------------------- #
# Synchronisation                                                              #
# --------------------------------------------------------------------------- #

def synchroniser(env, texte, essai):
    aujourd_hui = date.today()
    concours, ecartes = concours_de_l_agenda(texte, aujourd_hui)

    clients = {c["id"]: c["nom"] for c in tout_lire(env, "clients?select=id,nom")}
    id_shf = next((i for i, n in clients.items() if normaliser(n).startswith("shf")), None)
    activites = tout_lire(env, "activites?select=id,nom")
    id_dclik = next((a["id"] for a in activites if normaliser(a["nom"]).replace(" ", "").startswith("dclik")), None)
    try:
        passe = tout_lire(env, "evenements?select=client_id,lieu,date_debut&timetree_uid=is.null")
        existants = {e["timetree_uid"]: e for e in tout_lire(env,
            "evenements?select=id,timetree_uid,titre,titre_agenda,cle_agenda,date_debut,date_fin,lieu,client_id,"
            "client_devine,corrige_le,retire_agenda_le,etiquette_agenda&timetree_uid=not.is.null&source=eq.timetree")}
        regles = {r["cle"]: r for r in tout_lire(env, "regles_agenda?select=cle,client_id,lieu,nature") if r.get("nature", "concours") == "concours"}
    except urllib.error.HTTPError as e:
        if not essai:
            raise
        print("(migration 0048 pas encore passée : essai sur une base sans événements TimeTree ni règles)")
        passe = tout_lire(env, "evenements?select=client_id,lieu,date_debut")
        existants, regles = {}, {}

    nouveaux, modifs, vus = [], [], set()
    for c in concours:
        vus.add(c["uid"])
        cle = cle_agenda(c["titre"])
        regle = regles.get(cle)
        if regle:                                # appris de Julien : ça gagne sur tout
            cid, devine, lieu = regle.get("client_id"), False, regle.get("lieu") or lieu_de(c)
        else:
            cid, devine = deviner_client(c, passe, clients, id_shf)
            lieu = lieu_de(c)
        c["appris"] = bool(regle)
        champs = {"titre_agenda": c["titre"], "cle_agenda": cle, "date_debut": c["debut"].isoformat(),
                  "date_fin": c["fin"].isoformat(), "etiquette_agenda": c.get("etiquette")}
        ex = existants.get(c["uid"])
        if not ex:
            ligne = dict(champs, timetree_uid=c["uid"], source="timetree", titre=c["titre"], lieu=lieu,
                         client_id=cid, client_devine=devine, activite_id=id_dclik,
                         type_presta="captation_cso",
                         statut="realise" if c["fin"] < aujourd_hui else "planifie",
                         # un concours passé importé n'appelle pas de « suivi satisfaction »
                         suivi_fait=c["fin"] < aujourd_hui)
            ligne["_appris"] = c["appris"]
            nouveaux.append(ligne)
            continue
        patch = {k: v for k, v in champs.items() if (ex.get(k) or "") != (v or "")}
        if ex.get("titre") == ex.get("titre_agenda") and ex.get("titre") != c["titre"]:
            patch["titre"] = c["titre"]          # pas renommé à la main : on suit l'agenda
        if not ex.get("corrige_le"):             # corrigé par Julien : on n'y touche plus
            if (ex.get("lieu") or "") != (lieu or ""):
                patch["lieu"] = lieu
            if cid != ex.get("client_id") or devine != bool(ex.get("client_devine")):
                patch["client_id"], patch["client_devine"] = cid, devine
        if ex.get("retire_agenda_le"):
            patch["retire_agenda_le"] = None     # revenu dans l'agenda
        if patch:
            modifs.append((ex["id"], patch))

    maintenant = datetime.now(timezone.utc).isoformat()
    retires = [ex for uid, ex in existants.items()
               if uid not in vus and not ex.get("retire_agenda_le") and (ex.get("date_debut") or "") >= aujourd_hui.isoformat()]

    # --- compte rendu ---
    print("Agenda : %d concours retenus (journées entières dans la fenêtre), %d éléments écartés" % (len(concours), len(ecartes)))
    for n in nouveaux:
        qui = clients.get(n["client_id"], "client à préciser")
        origine = " (appris)" if n.get("_appris") else (" (deviné)" if n["client_devine"] else "")
        detail("  + %s → %s  %-24s lieu : %-18s client : %s%s" % (n["date_debut"], n["date_fin"], n["titre"][:24],
                                                                (n["lieu"] or "")[:18], qui, origine))
    for eid, p in modifs:
        detail("  ~ événement %s : %s" % (eid, ", ".join(sorted(p))))
    for ex in retires:
        detail("  - %s %s : n'est plus dans l'agenda (marqué, pas supprimé)" % (ex.get("date_debut"), ex.get("titre_agenda")))
    print("  %d ajouté(s), %d mis à jour, %d retiré(s) de l'agenda" % (len(nouveaux), len(modifs), len(retires)))
    if ecartes:
        detail("  écartés (rendez-vous à heure fixe ou sans date) : %s" % ", ".join(sorted(set(ecartes))[:10]))

    if essai:
        print("Essai : rien n'a été écrit.")
        return len(concours), None
    if nouveaux:
        for n in nouveaux:
            n.pop("_appris", None)
        requete(env, "POST", "evenements", nouveaux, prefer="return=minimal")
    for eid, p in modifs:
        requete(env, "PATCH", "evenements?id=eq.%s" % eid, p, prefer="return=minimal")
    for ex in retires:
        requete(env, "PATCH", "evenements?id=eq.%s" % ex["id"], {"retire_agenda_le": maintenant}, prefer="return=minimal")
    return len(concours), "%d concours lus, %d ajoutés, %d mis à jour, %d retirés de l'agenda" % (
        len(concours), len(nouveaux), len(modifs), len(retires))


# --------------------------------------------------------------------------- #
# Qui va sur quel concours                                                     #
# --------------------------------------------------------------------------- #

def mots_lieu(texte):
    # Trois lettres suffisent : les sigles des grands rendez-vous (GSP, CIR) sont des noms.
    return {m for m in normaliser(texte).split()
            if len(m) >= 3 and m not in MOTS_VIDES and m not in ROMAINS and not m.isdigit()}


def rapprocher(p, evenements, clients, stricte):
    """Le concours connu qui correspond à un événement du calendrier d'une personne.
    Il faut que les dates se chevauchent ET que le titre nomme le lieu, le client ou
    le concours. Pour un salarié, des dates strictement identiques suffisent aussi ;
    pour le calendrier privé de Julien (stricte), jamais : il faut le nom."""
    mots = mots_lieu(p["titre"] + " " + p.get("location", ""))
    meilleur, score_max, ex_aequo = None, 0, False
    for e in evenements:
        debut = e.get("date_debut")
        if not debut:
            continue
        fin = e.get("date_fin") or debut
        if fin < p["debut"].isoformat() or debut > p["fin"].isoformat():
            continue
        commun = mots & mots_lieu(" ".join([e.get("titre_agenda") or "", e.get("titre") or "",
                                            e.get("lieu") or "", clients.get(e.get("client_id"), "")]))
        memes_dates = debut == p["debut"].isoformat() and fin == p["fin"].isoformat()
        if not commun and (stricte or not memes_dates):
            continue
        score = len(commun) * 2 + (1 if memes_dates else 0)
        if score > score_max:
            meilleur, score_max, ex_aequo = e, score, False
        elif score == score_max:
            ex_aequo = True
    # Deux concours aussi plausibles l'un que l'autre : on ne tire pas au sort.
    return None if ex_aequo else meilleur


def synchroniser_personnes(env, personnes, essai):
    """personnes : liste de (nom, texte_ics). Retourne un résumé."""
    aujourd_hui = date.today()
    clients = {c["id"]: c["nom"] for c in tout_lire(env, "clients?select=id,nom")}
    debut_fenetre = (aujourd_hui - timedelta(days=FENETRE_PASSE)).isoformat()
    evenements = tout_lire(env, "evenements?select=id,titre,titre_agenda,lieu,client_id,date_debut,date_fin"
                                "&date_fin=gte.%s&source=neq.facture&supprime_le=is.null" % debut_fenetre)
    titres = {e["id"]: e.get("titre_agenda") or e.get("titre") for e in evenements}
    try:
        existantes = tout_lire(env, "affectations?select=id,evenement_id,personne,source,evenements(date_debut,date_fin)")
    except urllib.error.HTTPError:
        if not essai:
            raise
        print("(migration 0049 pas encore passée : essai sans affectations existantes)")
        existantes = []

    try:
        regles_toutes = {r["cle"]: r.get("nature") or "concours"
                         for r in tout_lire(env, "regles_agenda?select=cle,nature")}
    except urllib.error.HTTPError:
        regles_toutes = {}
    try:
        uuids_copies = {c["uuid"]: c["personne"] for c in tout_lire(env, "copies_timetree?select=uuid,personne")}
    except urllib.error.HTTPError:
        uuids_copies = {}
    resume = []
    for nom, texte in personnes:
        stricte = normaliser(nom) == "julien"
        items, _ = concours_de_l_agenda(texte, aujourd_hui)
        # Vérification des copies envoyées par La Régie : retrouvées, et avec quelle couleur.
        for p in items:
            if uuids_copies.get(p["uid"]) == nom:
                print("  %s : copie envoyée par La Régie retrouvée dans son agenda · couleur %s" % (nom, p.get("couleur") or "par défaut"))
        # Décompte anonyme, lisible dans le journal public : aucune date ni aucun titre.
        chevauchent = sum(1 for p in items if any(
            (e.get("date_debut") or "9999") <= p["fin"].isoformat()
            and (e.get("date_fin") or e.get("date_debut") or "") >= p["debut"].isoformat() for e in evenements))
        trouves = {}
        for p in items:
            e = rapprocher(p, evenements, clients, stricte)
            if e:
                trouves[e["id"]] = e
        a_garder = set(trouves)
        deja = {a["evenement_id"]: a for a in existantes if a["personne"] == nom}
        nouvelles = [eid for eid in a_garder if eid not in deja]
        # On ne retire que des affectations à venir : l'historique reste.
        # Une affectation posée à la main dans La Régie n'est jamais retirée par la synchro.
        perdues = [a for eid, a in deja.items() if eid not in a_garder and a.get("source") != "manuel"
                   and ((a.get("evenements") or {}).get("date_fin") or (a.get("evenements") or {}).get("date_debut") or "") >= debut_fenetre]
        # Calendrier privé : un événement de plusieurs jours qui ne tombe sur aucun
        # concours connu est PROPOSÉ à Julien (ou décidé d'office si une règle existe).
        propositions, auto = [], []
        if stricte:
            for p in items:
                if (p["fin"] - p["debut"]).days < 1 or p["fin"] < aujourd_hui:
                    continue
                if any(rapprocher(p, [e], clients, False) for e in evenements):
                    continue                     # déjà un concours à ces dates et à ce nom
                cle = cle_agenda(p["titre"])
                nature = regles_toutes.get(cle)
                if nature == "ignorer":
                    continue
                ligne = {"uid": p["uid"], "cle": cle, "personne": nom, "titre": p["titre"],
                         "date_debut": p["debut"].isoformat(), "date_fin": p["fin"].isoformat(),
                         "lieu": p.get("location") or None}
                (auto if nature == "concours" else propositions).append(ligne)
            print("  %s : %d proposition(s) de concours, %d créé(s) d'après tes réponses passées"
                  % (nom, len(propositions), len(auto)))
        for eid in sorted(a_garder, key=lambda i: trouves[i].get("date_debut") or ""):
            detail("  %s → %s %s" % (nom, trouves[eid].get("date_debut"), titres.get(eid)))
        print("  %s : %d journée(s) entière(s) lue(s) sur l'année, %d aux dates d'un concours → %d concours rattaché(s) (%d nouveau(x), %d retiré(s))"
              % (nom, len(items), chevauchent, len(a_garder), len(nouvelles), len(perdues)))
        resume.append("%s %d" % (nom, len(a_garder)))
        if essai:
            continue
        if propositions or auto:
            # ignore-duplicates : une proposition déjà tranchée garde sa décision.
            requete(env, "POST", "propositions_agenda?on_conflict=uid", propositions + auto,
                    prefer="resolution=ignore-duplicates,return=minimal")
            for ligne in auto:
                prop = requete(env, "GET", "propositions_agenda?uid=eq.%s&select=id,decision"
                               % urllib.parse.quote(ligne["uid"], safe=""))
                if prop and prop[0].get("decision") is None:
                    requete(env, "POST", "rpc/decider_proposition", {"p_id": prop[0]["id"], "p_concours": True})
        if nouvelles:
            requete(env, "POST", "affectations?on_conflict=evenement_id,personne",
                    [{"evenement_id": eid, "personne": nom} for eid in nouvelles],
                    prefer="resolution=merge-duplicates,return=minimal")
        for a in perdues:
            requete(env, "DELETE", "affectations?id=eq.%s" % a["id"], prefer="return=minimal")
    return "affectations : " + ", ".join(resume) if resume else ""


def main(argv):
    env = charger_env()
    if not env.get("SUPABASE_URL") or not env.get("SUPABASE_SERVICE_KEY"):
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY absents : rien à faire.")
        return 0
    if len(argv) >= 3 and argv[1] == "--echec":
        noter_synchro(env, False, argv[2])
        print("Échec noté : " + argv[2])
        return 0
    if len(argv) < 2 or not os.path.exists(argv[1]):
        print("Usage : timetree_sync.py dossier/|saison.ics [--essai] | --echec \"message\"")
        return 1
    essai = "--essai" in argv
    if os.path.isdir(argv[1]):
        index = json.load(open(os.path.join(argv[1], "index.json"), encoding="utf-8"))
        lire = lambda f: open(os.path.join(argv[1], f), encoding="utf-8").read()
        concours = [lire(f) for f, role in index.items() if role == "concours"]
        personnes = [(role, lire(f)) for f, role in index.items() if role != "concours"]
    else:
        concours, personnes = [open(argv[1], encoding="utf-8").read()], []
    try:
        nb, messages = 0, []
        for texte in concours:
            n, msg = synchroniser(env, texte, essai)
            nb += n
            if msg:
                messages.append(msg)
        if personnes:
            msg = synchroniser_personnes(env, personnes, essai)
            if msg:
                messages.append(msg)
        if not essai:
            noter_synchro(env, True, " · ".join(messages), nb=nb)
            print("Synchronisé.")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print("Écriture refusée par Supabase : HTTP %s %s" % (e.code, detail))
        if "--essai" not in argv:
            try:
                noter_synchro(env, False, "Supabase HTTP %s : %s" % (e.code, detail))
            except Exception:
                pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
