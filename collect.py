#!/usr/bin/env python3
# La Régie — collecteur mail (LECTURE SEULE IMAP -> Supabase).
# Lit comptes.json (boîtes + mdp) et range les messages dans la table `mails`.
# Détecte les newsletters (List-Unsubscribe) + extrait le lien de désabo.
# Stdlib uniquement (imaplib/email/json/urllib). Aucun mot de passe ici :
#   - mdp des boîtes -> comptes.json   (privé, gitignored)
#   - clé Supabase   -> .env           (privé, gitignored)
import imaplib, email, json, sys, os, re, ssl, hashlib, urllib.request, urllib.parse, html as htmlmod
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime


def extract_and_upload_pj(env, msg, msgid):
    # Récupère les pièces jointes d'un mail et les envoie dans le bucket Storage 'mail-pj'.
    # Renvoie les métadonnées [{nom, type, taille, path}]. Défensif : n'échoue jamais.
    url = (env.get("SUPABASE_URL") or "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY") or ""
    if not url or not key:
        return []
    folder = hashlib.md5((msgid or "x").encode("utf-8", "replace")).hexdigest()
    pjs = []
    try:
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            raw_fn = part.get_filename()
            if "attachment" not in disp and not raw_fn:
                continue          # partie de corps inline sans fichier -> ignorée
            if not raw_fn:
                continue
            fn = dec(raw_fn)
            try:
                data = part.get_payload(decode=True)
            except Exception:
                data = None
            if not data or len(data) > 20 * 1024 * 1024:   # ignore > 20 Mo
                continue
            safe = re.sub(r"[^\w.\-]+", "_", fn)[:100] or "fichier"
            path = f"{folder}/{safe}"
            ct = part.get_content_type() or "application/octet-stream"
            try:
                req = urllib.request.Request(
                    f"{url}/storage/v1/object/mail-pj/{urllib.parse.quote(path)}",
                    data=data, method="POST",
                    headers={"apikey": key, "Authorization": f"Bearer {key}",
                             "Content-Type": ct, "x-upsert": "true"})
                with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as r:
                    if 200 <= r.status < 300:
                        pjs.append({"nom": fn, "type": ct, "taille": len(data), "path": path})
            except Exception as e:
                print("   ⚠️ upload PJ échoué :", fn, e)
    except Exception as e:
        print("   ⚠️ extraction PJ échouée :", e)
    return pjs

HERE = os.path.dirname(os.path.abspath(__file__))
PER_BOX = 30  # nb de derniers mails relevés par boîte

# Postgres refuse le caractère NUL et les caractères de contrôle dans du texte.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(s):
    return _CTRL.sub("", s) if isinstance(s, str) else s


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
    return clean(out.replace("\n", " ").replace("\r", " ").strip())


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


def body_text(msg):
    # Texte complet du message (sauts de ligne conservés pour la lecture).
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
                        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
                        text = re.sub(r"<[^>]+>", " ", html)
                        break
                    except Exception:
                        continue
    else:
        try:
            text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
        # Mono-part HTML : retirer les balises (sinon aperçu = code HTML brut).
        if msg.get_content_type() == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)  # &nbsp; &amp; &#39; ... -> caractères lisibles
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n\s*", "\n\n", text).strip()
    return clean(text)


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
    # Déplace en Corbeille les mails marqués 'a_supprimer', ET réessaie les 'suppr_echec'
    # (échecs passés). Jamais de suppression définitive : sans Corbeille atteignable on garde 'suppr_echec'.
    to_del = supa_get(env, "mails?statut=in.(a_supprimer,suppr_echec)&select=id,boite,message_id")
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
            statut = "suppr_echec"
            try:
                ids = []
                if mid.startswith("<"):
                    for q in (mid, mid.strip("<>")):  # certains serveurs cherchent sans les chevrons
                        typ, data = M.search(None, "HEADER", "Message-ID", q)
                        ids = data[0].split() if data and data[0] else []
                        if ids:
                            break
                if not ids:
                    statut = "supprime"  # introuvable dans INBOX -> déjà retiré, on ne le réaffiche plus
                else:
                    moved = False
                    for trash in ("Corbeille", "INBOX.Corbeille", "Trash", "INBOX.Trash", "Deleted Messages"):
                        try:
                            if all(M.copy(num, trash)[0] == "OK" for num in ids):
                                moved = True
                                break
                        except Exception:
                            continue
                    if moved:
                        for num in ids:
                            M.store(num, "+FLAGS", "\\Deleted")
                        M.expunge()
                        statut = "supprime"
                    # trouvé mais aucune corbeille n'accepte la copie -> reste 'suppr_echec'
            except Exception as e:
                print(f"  ⚠️ suppression {mid[:30]} : {e}")
            supa_patch(env, f"mails?id=eq.{m['id']}", {"statut": statut})
            total += 1 if statut == "supprime" else 0
        try:
            M.logout()
        except Exception:
            pass
    if total:
        print(f"🗑️  {total} mail(s) déplacé(s) en Corbeille.")


def discord_post(webhook, title, content):
    # Poste dans le forum Discord (User-Agent obligatoire, Cloudflare bloque sinon ; thread_name requis).
    if not webhook:
        return False
    body = json.dumps({"thread_name": title[:90], "content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (LaRegie)"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:
        print("  ⚠️ Discord:", e)
        return False


def check_bank_expiry(env):
    # Notifie sur Discord quand un consentement bancaire Enable Banking approche des 89 j (ou a expiré).
    import datetime
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook:
        return
    rows = supa_get(env, "bank_connexions?source=eq.enablebanking&exp_notifiee=eq.false&select=id,institution_nom,valid_until")
    now = datetime.datetime.now(datetime.timezone.utc)
    for c in rows or []:
        vu = c.get("valid_until")
        if not vu:
            continue
        try:
            d = datetime.datetime.fromisoformat(str(vu).replace("Z", "+00:00"))
        except Exception:
            continue
        days = (d - now).total_seconds() / 86400
        if days >= 7:
            continue  # encore loin
        nom = c.get("institution_nom") or "Banque"
        if days < 0:
            title = "⚠️ Banque — consentement EXPIRÉ"
            msg = f"La connexion **{nom}** a expiré. Ouvre La Régie → Pointage → « + Banque » pour la renouveler."
        else:
            title = "⏳ Banque — consentement bientôt expiré"
            msg = f"La connexion **{nom}** expire le {d.date().isoformat()} (dans {int(days)} j). Renouvelle-la dans La Régie (« + Banque »)."
        if discord_post(webhook, title, msg):
            supa_patch(env, f"bank_connexions?id=eq.{c['id']}", {"exp_notifiee": True})


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
    for r in supa_get(env, "mails?select=message_id,categorie,corrige,suggestion_suppr,pieces_jointes,is_newsletter"):
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
                addr = clean(addr)
                is_news = bool(msg.get("List-Unsubscribe") or msg.get("List-Id"))
                sujet = dec(msg.get("Subject")) or "(sans objet)"
                full = body_text(msg)
                snip = re.sub(r"\s+", " ", full)[:320]
                corps = full[:15000]
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
                    pj = prev.get("pieces_jointes")          # déjà extraites -> on garde
                    is_news = bool(prev.get("is_newsletter"))  # préserve la reclassification manuelle
                elif is_news:
                    cat = "newsletter"
                    pj = extract_and_upload_pj(env, msg, msgid)
                elif api_key:
                    res = claude_classify(api_key, exemples, suppr_ex, addr, sujet, snip)
                    cat, sug = res["categorie"], res["supprimer"]
                    pj = extract_and_upload_pj(env, msg, msgid)
                else:
                    cat = categorize_fallback(sujet, snip)
                    pj = extract_and_upload_pj(env, msg, msgid)
                rows.append({
                    "compte": label,
                    "boite": acc["email"],
                    "message_id": msgid,
                    "from_addr": addr,
                    "from_name": dec(name),
                    "sujet": sujet,
                    "date_recue": date_iso,
                    "snippet": snip,
                    "corps": corps,
                    "categorie": cat,
                    "suggestion_suppr": sug,
                    "is_newsletter": is_news,
                    "unsubscribe_url": unsubscribe_link(msg.get("List-Unsubscribe")),
                    "pieces_jointes": pj,
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

    # Inscriptions d'un nouveau compte (notifications du site D'clik Agency) : pas de réponse
    # attendue -> rangées automatiquement en 'cavalier' et marquées 'traité'.
    supa_patch(env, "mails?sujet=ilike.*inscription*nouveau*compte*&statut=eq.a_traiter",
               {"categorie": "cavalier", "is_newsletter": False, "statut": "traite"})

    # Demandes de contact du site = cavaliers À QUI RÉPONDRE -> 'cavalier', à traiter,
    # pas newsletter (l'envoi répondra à l'email du corps, pas au noreply@).
    supa_patch(env, "mails?sujet=ilike.*demande*contact*&is_newsletter=eq.true",
               {"categorie": "cavalier", "is_newsletter": False, "statut": "a_traiter"})

    # Traite les suppressions demandées par Julien (déplacement en Corbeille).
    process_deletions(env, accounts)

    # Prévient sur Discord si un consentement bancaire arrive à échéance (89 j) ou a expiré.
    check_bank_expiry(env)

    print(f"\n✅ Terminé : {total_push} mails dans la Régie. Ouvre le cockpit, onglet Mails.")


if __name__ == "__main__":
    main()
