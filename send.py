# -*- coding: utf-8 -*-
# La Régie — envoi des réponses validées par Julien.
# Déclenché par le cockpit (workflow_dispatch). Lit les mails marqués
# envoi_demande=true, envoie le brouillon (validé/corrigé) via le SMTP de la boîte,
# marque le mail répondu, et mémorise la réponse envoyée pour l'apprentissage du ton.
#
# Stdlib uniquement (smtplib/email/json/urllib). Mots de passe : COMPTES_JSON (secret).

import os, json, smtplib, ssl, urllib.request, urllib.error
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


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
    s = (server or "imap.ionos.fr").replace("imap", "smtp")
    return s


def re_subject(sujet):
    s = (sujet or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def send_one(acc, mail):
    dest = (mail.get("from_addr") or "").strip()
    body = (mail.get("brouillon") or "").strip()
    if not dest or not body:
        return False, "destinataire ou brouillon vide"

    msg = EmailMessage()
    msg["From"] = formataddr((acc.get("label") or "", acc["email"]))
    msg["To"] = dest
    msg["Subject"] = re_subject(mail.get("sujet"))
    ref = (mail.get("message_id") or "").strip()
    if ref:
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg["Message-ID"] = make_msgid(domain=acc["email"].split("@")[-1])
    msg.set_content(body)

    host = smtp_host(acc.get("server"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, 465, context=ctx, timeout=30) as s:
        s.login(acc["email"], acc["password"])
        s.send_message(msg)
    return True, "ok"


def main():
    e = env_supa()
    if not e["url"] or not e["key"]:
        print("❌ SUPABASE_URL / SUPABASE_SERVICE_KEY absents.")
        return
    accounts = load_accounts()
    acc_by_email = {a["email"]: a for a in accounts}
    acc_by_label = {a.get("label", a["email"]): a for a in accounts}

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
        try:
            ok, why = send_one(acc, mail)
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
