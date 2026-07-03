# -*- coding: utf-8 -*-
# La Régie — envoi des réponses validées par Julien.
# Déclenché par le cockpit (workflow_dispatch). Lit les mails marqués
# envoi_demande=true, envoie le brouillon (validé/corrigé) via le SMTP de la boîte,
# marque le mail répondu, et mémorise la réponse envoyée pour l'apprentissage du ton.
#
# Envoi en multipart : texte simple + HTML (avec logo embarqué en CID si la boîte
# en a un). Stdlib uniquement. Mots de passe : COMPTES_JSON (secret).

import os, re, json, smtplib, ssl, imaplib, time, urllib.request, urllib.error
import html as htmllib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate


def find_sent_folder(M):
    # Cherche le dossier "Envoyés" : d'abord le flag \Sent, sinon les noms courants.
    try:
        typ, data = M.list()
        for line in (data or []):
            s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\Sent" in s:
                m = re.search(r'"([^"]*)"\s*$', s) or re.search(r'([^ ]+)\s*$', s)
                if m:
                    return m.group(1)
    except Exception:
        pass
    for name in ["Sent", "Envoyés", "INBOX.Sent", "Sent Items", "INBOX.Sent Items", "[Gmail]/Sent Mail"]:
        try:
            typ, _ = M.select(f'"{name}"')
            if typ == "OK":
                return name
        except Exception:
            pass
    return None


def save_to_sent(acc, raw_bytes):
    # Dépose une copie de l'email dans le dossier "Envoyés" de la boîte (IMAP APPEND),
    # pour qu'il apparaisse dans Mail et serve de preuve d'envoi.
    try:
        M = imaplib.IMAP4_SSL(acc.get("server", "imap.ionos.fr"), acc.get("port", 993),
                              ssl_context=ssl.create_default_context())
        M.login(acc["email"], acc["password"])
        folder = find_sent_folder(M)
        if folder:
            M.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), raw_bytes)
        try:
            M.logout()
        except Exception:
            pass
        return folder
    except Exception as e:
        print("   ⚠️ copie 'Envoyés' échouée :", e)
        return None


def load_accounts():
    if os.environ.get("COMPTES_JSON"):
        return json.loads(os.environ["COMPTES_JSON"])
    p = os.path.join(os.path.dirname(__file__), "comptes.json")
    return json.load(open(p)) if os.path.exists(p) else []


def env_supa():
    return {
        "url": (os.environ.get("SUPABASE_URL") or "").rstrip("/"),
        "key": os.environ.get("SUPABASE_SERVICE_KEY") or "",
    }


def supa_get(e, path):
    req = urllib.request.Request(f"{e['url']}/rest/v1/{path}", headers={
        "apikey": e["key"], "Authorization": f"Bearer {e['key']}",
    })
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def supa_write(e, path, payload, method="PATCH"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{e['url']}/rest/v1/{path}", data=data, method=method, headers={
        "apikey": e["key"], "Authorization": f"Bearer {e['key']}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=30) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as ex:
        print("   ⚠️ Supabase", ex.code, ex.read().decode("utf-8", "replace")[:200])
        return False


def smtp_host(server):
    # imap.ionos.fr -> smtp.ionos.fr, imap.gmail.com -> smtp.gmail.com, etc.
    return (server or "imap.ionos.fr").replace("imap", "smtp")


def re_subject(sujet):
    s = (sujet or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def vrai_destinataire(from_addr, corps):
    # Formulaire de contact du site : l'expéditeur est noreply@, la vraie adresse
    # du cavalier est dans le corps ("Votre adresse e-mail : ..."). On répond à celle-là.
    fa = (from_addr or "").lower()
    if "noreply" not in fa and "no-reply" not in fa:
        return from_addr
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", corps or "")
    ext = [e for e in emails if "dclik-agency.com" not in e.lower()
           and "noreply" not in e.lower() and "no-reply" not in e.lower()]
    return ext[0] if ext else from_addr


def logo_path(fn):
    if not fn:
        return None
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos", fn)
    return p if os.path.exists(p) else None


def htmlify(text):
    # Texte -> HTML : échappe, transforme liens et emails en <a>, sauts de ligne en <br>.
    esc = htmllib.escape(text)
    esc = re.sub(
        r'((?:https?://|www\.)[^\s<]+)',
        lambda m: '<a href="%s">%s</a>' % (
            m.group(1) if m.group(1).startswith("http") else "https://" + m.group(1), m.group(1)),
        esc,
    )
    esc = re.sub(r'([\w.+-]+@[\w-]+\.[\w.-]+)', r'<a href="mailto:\1">\1</a>', esc)
    return esc.replace("\n", "<br>\n")


def send_one(acc, mail, sig):
    dest = (vrai_destinataire(mail.get("from_addr"), mail.get("corps")) or "").strip()
    body = (mail.get("brouillon") or "").strip()
    if not dest or not body:
        return False, "destinataire ou brouillon vide"
    sig = sig or {}
    text_sig = (sig.get("texte") or "").strip()
    html_sig = (sig.get("html") or "").strip()
    logo_fn = sig.get("logo")           # image simple héritée (bannière seule)
    images = sig.get("images") or {}    # map { cid: fichier.png } embarquée dans le HTML
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except Exception:
            images = {}

    msg = EmailMessage()
    msg["From"] = formataddr((acc.get("label") or "", acc["email"]))
    msg["To"] = dest
    msg["Subject"] = re_subject(mail.get("sujet"))
    ref = (mail.get("message_id") or "").strip()
    if ref:
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg["Message-ID"] = make_msgid(domain=acc["email"].split("@")[-1])
    msg["Date"] = formatdate(localtime=True)

    # Partie TEXTE (repli universel) : corps + signature texte.
    plain = body + (("\n\n" + text_sig) if text_sig else "")
    msg.set_content(plain)

    # Partie HTML : corps + signature (HTML riche si dispo, sinon texte).
    parts = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
             'sans-serif;font-size:14px;color:#222;line-height:1.5;">', htmlify(body)]
    if html_sig:
        parts.append('<br><br>' + html_sig)
    elif text_sig:
        parts.append('<br><br>' + htmlify(text_sig))
    # Image simple héritée (bannière seule, si pas de map images).
    legacy_cid = None
    lp_legacy = logo_path(logo_fn) if (logo_fn and not images) else None
    if lp_legacy:
        legacy_cid = make_msgid(domain=acc["email"].split("@")[-1])
        parts.append('<br><br><img src="cid:%s" width="600" '
                     'style="max-width:100%%;height:auto;display:block;" alt="">' % legacy_cid.strip("<>"))
    parts.append('</div>')
    msg.add_alternative("".join(parts), subtype="html")

    # Images embarquées (CID) : la map { cid: fichier } référencée dans le HTML riche,
    # + l'éventuelle image simple héritée. Toutes attachées à la partie HTML.
    html_part = msg.get_payload()[-1]
    for cidname, fn in images.items():
        lp = logo_path(fn)
        if lp:
            with open(lp, "rb") as fh:
                html_part.add_related(fh.read(), maintype="image", subtype="png", cid="<%s>" % cidname)
    if lp_legacy:
        with open(lp_legacy, "rb") as fh:
            html_part.add_related(fh.read(), maintype="image", subtype="png", cid=legacy_cid)

    host = smtp_host(acc.get("server"))
    with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(acc["email"], acc["password"])
        s.send_message(msg)
    # Copie dans "Envoyés" pour que ça apparaisse dans Mail (+ preuve d'envoi).
    folder = save_to_sent(acc, msg.as_bytes())
    return True, ("ok, copié dans " + folder if folder else "ok (sans copie Envoyés)")


def main():
    e = env_supa()
    if not e["url"] or not e["key"]:
        print("❌ SUPABASE_URL / SUPABASE_SERVICE_KEY absents.")
        return
    accounts = load_accounts()
    acc_by_email = {a["email"]: a for a in accounts}
    acc_by_label = {a.get("label", a["email"]): a for a in accounts}

    # Signatures par boîte (texte repli + HTML riche + logo).
    sigs = {}
    try:
        for s in supa_get(e, "signatures?select=compte,texte,html,logo,images"):
            sigs[s["compte"]] = s
    except Exception:
        pass

    pending = supa_get(e, "mails?envoi_demande=eq.true&repondu=eq.false"
                          "&select=id,compte,boite,from_addr,message_id,sujet,brouillon,categorie,corps")
    print(f"✉️  {len(pending)} réponse(s) à envoyer.")
    for mail in pending:
        # la boîte réceptrice = champ `boite` (email) ; secours par `compte` (label).
        acc = acc_by_email.get(mail.get("boite")) or acc_by_label.get(mail.get("compte"))
        if not acc or not acc.get("password"):
            print(f"   ⏭️  mail {mail['id']} : compte introuvable / sans mot de passe.")
            supa_write(e, f"mails?id=eq.{mail['id']}", {"envoi_demande": False})
            continue
        sig = sigs.get(mail.get("boite"))
        try:
            ok, why = send_one(acc, mail, sig)
        except Exception as ex:
            ok, why = False, str(ex)
        if ok:
            supa_write(e, f"mails?id=eq.{mail['id']}",
                       {"repondu": True, "envoi_demande": False, "statut": "traite"})
            supa_write(e, "reponse_exemples", [{
                "mail_id": mail["id"], "categorie": mail.get("categorie"),
                "from_addr": mail.get("from_addr"), "sujet": mail.get("sujet"),
                "extrait": (mail.get("corps") or "")[:400], "reponse": mail.get("brouillon"),
            }], method="POST")
            print(f"   ✅ envoyé -> {mail.get('from_addr')} (mail {mail['id']})")
        else:
            # Pas de renvoi en boucle : on retire la demande, le mail reste à traiter.
            supa_write(e, f"mails?id=eq.{mail['id']}", {"envoi_demande": False})
            print(f"   ❌ échec mail {mail['id']} : {why}")


if __name__ == "__main__":
    main()
