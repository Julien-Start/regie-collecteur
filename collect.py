#!/usr/bin/env python3
# La Régie — collecteur mail (LECTURE SEULE IMAP -> Supabase).
# Lit comptes.json (boîtes + mdp) et range les messages dans la table `mails`.
# Détecte les newsletters (List-Unsubscribe) + extrait le lien de désabo.
# Stdlib uniquement (imaplib/email/json/urllib). Aucun mot de passe ici :
#   - mdp des boîtes -> comptes.json   (privé, gitignored)
#   - clé Supabase   -> .env           (privé, gitignored)
import imaplib, email, json, sys, os, re, ssl, urllib.request
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PER_BOX = 30  # nb de derniers mails relevés par boîte


def dec(s):
    if not s:
        return ""
    out = ""
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try:
                out += txt.decode(enc or "utf-8", "replace")
            except Exception:
                out += txt.decode("latin-1", "replace")
        else:
            out += txt
    return out.replace("\n", " ").replace("\r", " ").strip()


def load_json(name):
    # Hébergé (GitHub Actions, etc.) : COMPTES_JSON dans l'environnement.
    if name == "comptes.json" and os.environ.get("COMPTES_JSON"):
        return json.loads(os.environ["COMPTES_JSON"])
    # Local : fichier sur le Mac.
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def load_env():
    env = {}
    # Fichier VISIBLE en priorité (cles-supabase.txt), repli sur .env (caché).
    for name in ("cles-supabase.txt", ".env"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            break
    # Hébergé : les variables d'environnement priment (secrets GitHub, etc.).
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def unsubscribe_link(value):
    if not value:
        return ""
    m = re.search(r"<(https?://[^>]+)>", value)
    if m:
        return m.group(1)
    m = re.search(r"<mailto:([^>]+)>", value)
    return ("mailto:" + m.group(1)) if m else ""


def body_snippet(msg, limit=320):
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    break
                except Exception:
                    continue
        if not text:  # repli html
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        text = re.sub(r"<[^>]+>", " ", html)
                        break
                    except Exception:
                        continue
    else:
        try:
            text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def categorize_fallback(sujet, snippet):
    # Repli par mots-clés pour les expéditeurs encore inconnus (avant correction).
    blob = f"{sujet} {snippet}".lower()
    if any(k in blob for k in ("facture", "devis", "règlement", "paiement", "acompte")):
        return "facture"
    if any(k in blob for k in ("vimeo", "vidéo", "video", "accès", "mot de passe", "identifiant", "replay", "lien")):
        return "cavalier"
    return "autre"


def supa_get(env, path):
    url = env.get("SUPABASE_URL", "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY", "")
    req = urllib.request.Request(f"{url}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


CATEGORIES = (
    "cavalier", "facture", "devis", "client", "partenaire", "fournisseur",
    "admin", "immobilier", "technique", "rdv", "alcove", "perso", "spam", "autre",
)


def claude_classify(api_key, exemples, suppr_ex, from_addr, sujet, snippet):
    # Classe sur le CONTENU. Les corrections de Julien servent d'exemples (few-shot).
    fewshot = "\n".join(
        f"- de:{e.get('from_addr','')} | objet:{(e.get('sujet') or '')[:80]} -> {e.get('categorie')}"
        for e in (exemples or [])[:25]
    )
    system = (
        "Tu classes les emails pro de Julien (D'clik Agency, captation vidéo de concours équestres) "
        "en UNE seule catégorie parmi : " + ", ".join(CATEGORIES) + ".\n"
        "Définitions : "
        "cavalier = demande d'un cavalier/spectateur (accès vidéo, identifiants, replay d'un concours) ; "
        "facture = facture reçue, paiement, virement, acompte, relance d'argent ; "
        "devis = demande de devis, nouveau prospect qui se renseigne, appel d'offres ; "
        "client = échange avec un organisateur/client existant sur un événement (logistique, planning, infos) ; "
        "partenaire = sponsor, publicité, partenariat commercial ; "
        "fournisseur = prestataire, achat, commande, abonnement (hébergeur, matériel, logiciel) ; "
        "admin = administratif, impôts, URSSAF, banque, assurance, juridique, organismes officiels ; "
        "immobilier = SCI, locataire, loyer, bail, notaire, agence immobilière ; "
        "technique = problème ou support technique d'un site/plateforme (Vimeo, WordPress, bug, panne, mot de passe d'un outil) ; "
        "rdv = prise de rendez-vous, proposition de créneau, invitation agenda ; "
        "alcove = ce qui concerne l'activité L'Alcove de Julien ; "
        "perso = personnel, privé, famille, amis ; "
        "spam = démarchage non sollicité, indésirable, arnaque ; "
        "autre = le reste.\n"
        "Classe sur le CONTENU, pas seulement l'expéditeur (un même expéditeur peut varier).\n"
        + ("Exemples de classements de Julien :\n" + fewshot + "\n" if fewshot else "")
    )
    # Apprentissage des suppressions (sécurité : juste une SUGGESTION, jamais auto).
    suppr_fewshot = "\n".join(
        f"- de:{e.get('from_addr','')} | objet:{(e.get('sujet') or '')[:80]}"
        for e in (suppr_ex or [])[:20]
    )
    if suppr_fewshot:
        system += (
            "\nJulien a déjà SUPPRIMÉ des mails ressemblant à ceci :\n" + suppr_fewshot + "\n"
            "Mets supprimer=true UNIQUEMENT si ce mail ressemble FORTEMENT à ces exemples ; sinon false."
        )
    else:
        system += "\nAucun exemple de suppression connu : mets toujours supprimer=false."
    system += '\nRéponds en JSON strict, rien d\'autre : {"categorie":"<un mot de la liste>","supprimer":true|false}'

    user = f"De : {from_addr}\nObjet : {sujet}\nDébut du message : {(snippet or '')[:400]}"
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 40,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
        txt = (j.get("content", [{}])[0].get("text", "") or "").strip()
        cat, suppr = "autre", False
        try:
            obj = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            c = str(obj.get("categorie", "")).strip().lower()
            cat = c if c in CATEGORIES else "autre"
            suppr = bool(obj.get("supprimer", False))
        except Exception:
            low = txt.lower()
            cat = next((c for c in CATEGORIES if c in low), "autre")
        return {"categorie": cat, "supprimer": suppr}
    except Exception:
        return {"categorie": categorize_fallback(sujet, snippet), "supprimer": False}


def supa_upsert(env, rows):
    url = env.get("SUPABASE_URL", "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return False, "SUPABASE_URL / SUPABASE_SERVICE_KEY absents du .env"
    endpoint = f"{url}/rest/v1/mails?on_conflict=message_id"
    data = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST", headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
    except Exception as e:
        return False, str(e)


def supa_patch(env, path, payload):
    url = env.get("SUPABASE_URL", "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY", "")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method="PATCH", headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def process_deletions(env, accounts):
    # Supprime (DÉPLACE EN CORBEILLE) les mails que Julien a marqués 'a_supprimer'.
    # Jamais de suppression définitive : si pas de Corbeille atteignable, on n'efface PAS.
    to_del = supa_get(env, "mails?statut=eq.a_supprimer&select=id,boite,message_id")
    if not to_del:
        return
    acc_by_email = {a["email"]: a for a in accounts}
    by_box = {}
    for m in to_del:
        by_box.setdefault(m.get("boite"), []).append(m)

    total = 0
    for boite, mails in by_box.items():
        acc = acc_by_email.get(boite)
        if not acc or not acc.get("password"):
            continue
        try:
            M = imaplib.IMAP4_SSL(acc.get("server", "imap.ionos.fr"), acc.get("port", 993))
            M.login(acc["email"], acc["password"])
            M.select("INBOX")  # mode écriture
        except Exception as e:
            print(f"  ⚠️ suppression : connexion {boite} échouée : {e}")
            continue
        for m in mails:
            mid = (m.get("message_id") or "").strip()
            moved = False
            if mid.startswith("<"):
                try:
                    typ, data = M.search(None, "HEADER", "Message-ID", mid)
                    ids = data[0].split() if data and data[0] else []
                    if ids:
                        for trash in ("Trash", "INBOX.Trash", "Corbeille", "INBOX.Corbeille"):
                            try:
                                if all(M.copy(num, trash)[0] == "OK" for num in ids):
                                    moved = True
                                    break
                            except Exception:
                                continue
                        if moved:  # uniquement si bien copié en Corbeille
                            for num in ids:
                                M.store(num, "+FLAGS", "\\Deleted")
                            M.expunge()
                except Exception as e:
                    print(f"  ⚠️ suppression {mid[:30]} : {e}")
            supa_patch(env, f"mails?id=eq.{m['id']}", {"statut": "supprime" if moved else "suppr_echec"})
            total += 1 if moved else 0
        try:
            M.logout()
        except Exception:
            pass
    if total:
        print(f"🗑️  {total} mail(s) déplacé(s) en Corbeille.")


def main():
    accounts = load_json("comptes.json")
    if not accounts:
        print("❌ comptes.json introuvable. Duplique comptes.example.json et remplis les mdp.")
        sys.exit(1)
    env = load_env()
    if not env.get("SUPABASE_URL"):
        print("❌ .env introuvable ou incomplet. Duplique .env.example en .env et remplis les clés Supabase.")
        sys.exit(1)

    # Mails déjà connus : préserve tes corrections + évite de reclasser inutilement.
    existing = {}
    for r in supa_get(env, "mails?select=message_id,categorie,corrige,suggestion_suppr"):
        if r.get("message_id"):
            existing[r["message_id"]] = r
    # Tes corrections passées = exemples pour guider Claude.
    exemples = supa_get(env, "tri_exemples?select=from_addr,sujet,categorie&order=created_at.desc&limit=25")
    # Tes suppressions passées = exemples pour SUGGÉRER (jamais supprimer auto).
    suppr_ex = supa_get(env, "suppr_exemples?select=from_addr,sujet&order=created_at.desc&limit=20")
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if api_key:
        print(f"🧠 Tri Claude actif ({len(exemples)} exemples appris).")
    else:
        print("ℹ️  Pas de clé Claude : tri par mots-clés (ajoute ANTHROPIC_API_KEY pour le tri intelligent).")

    total_push = 0
    for acc in accounts:
        label = acc.get("label", acc["email"])
        if not acc.get("password"):
            print(f"⏭️  {label} : pas de mot de passe, ignorée.")
            continue
        print(f"\n📬 {label}  <{acc['email']}>")
        try:
            M = imaplib.IMAP4_SSL(acc.get("server", "imap.ionos.fr"), acc.get("port", 993))
            M.login(acc["email"], acc["password"])
        except Exception as e:
            print("  ❌ connexion échouée :", e)
            continue
        rows = []
        try:
            M.select("INBOX", readonly=True)
            typ, data = M.search(None, "ALL")
            ids = data[0].split()
            for num in ids[-PER_BOX:][::-1]:
                typ, md = M.fetch(num, "(BODY.PEEK[])")
                if not md or not md[0]:
                    continue
                msg = email.message_from_bytes(md[0][1])
                name, addr = parseaddr(msg.get("From", ""))
                is_news = bool(msg.get("List-Unsubscribe") or msg.get("List-Id"))
                sujet = dec(msg.get("Subject")) or "(sans objet)"
                snip = body_snippet(msg)
                try:
                    dt = parsedate_to_datetime(msg.get("Date"))
                    date_iso = dt.isoformat() if dt else None
                except Exception:
                    date_iso = None
                msgid = (msg.get("Message-ID") or f"{acc['email']}|{num.decode()}|{sujet[:40]}").strip()
                prev = existing.get(msgid)
                sug = False
                if prev:
                    cat = prev.get("categorie") or "autre"   # déjà classé/corrigé -> on garde
                    sug = bool(prev.get("suggestion_suppr"))  # on préserve la suggestion
                elif is_news:
                    cat = "newsletter"
                elif api_key:
                    res = claude_classify(api_key, exemples, suppr_ex, addr, sujet, snip)
                    cat, sug = res["categorie"], res["supprimer"]
                else:
                    cat = categorize_fallback(sujet, snip)
                rows.append({
                    "compte": label,
                    "boite": acc["email"],
                    "message_id": msgid,
                    "from_addr": addr,
                    "from_name": dec(name),
                    "sujet": sujet,
                    "date_recue": date_iso,
                    "snippet": snip,
                    "categorie": cat,
                    "suggestion_suppr": sug,
                    "is_newsletter": is_news,
                    "unsubscribe_url": unsubscribe_link(msg.get("List-Unsubscribe")),
                })
        finally:
            try:
                M.logout()
            except Exception:
                pass

        if rows:
            ok, info = supa_upsert(env, rows)
            if ok:
                total_push += len(rows)
                news = sum(1 for r in rows if r["is_newsletter"])
                print(f"  ✅ {len(rows)} mails rangés ({news} newsletters)")
            else:
                print(f"  ❌ envoi Supabase échoué : {info}")

    # Traite les suppressions demandées par Julien (déplacement en Corbeille).
    process_deletions(env, accounts)

    print(f"\n✅ Terminé : {total_push} mails dans la Régie. Ouvre le cockpit, onglet Mails.")


if __name__ == "__main__":
    main()
