#!/usr/bin/env python3
# La Régie — envoyer à Julien, par mail, l'export complet d'un calendrier TimeTree.
#
# Sert à copier un calendrier TimeTree dans un autre : TimeTree ne sait pas exporter,
# mais sait copier un calendrier de l'iPhone. Le fichier .ics arrive par mail, Julien
# l'ouvre sur l'iPhone (« Tout ajouter » dans un calendrier provisoire), puis l'importe
# dans TimeTree vers le calendrier voulu.
#
#   python3 timetree_envoi.py dclik-agency.ics j.rouyer@dclik-agency.com
#
# Le mail part de la boîte de destination vers elle-même : aucune adresse extérieure.
# Réutilise l'envoi du collecteur (send.py : comptes, message, SMTP, copie dans Envoyés).
import sys, re

import send


CORPS = """Bonjour Julien,

Voici l'export complet de ton calendrier TimeTree « D'clik agency » : {n} événement(s), en pièce jointe.

Pour le copier dans un autre calendrier TimeTree :

1. Sur l'iPhone, ouvre la pièce jointe dans Mail, touche « Tout ajouter », et range les événements dans un calendrier PROVISOIRE (crée-en un dans l'app Calendrier, par exemple « Import D'clik »), pas dans ton calendrier habituel.
2. Dans TimeTree, importe ce calendrier du téléphone vers le calendrier TimeTree de destination (Réglages de TimeTree → import / copie des calendriers du téléphone).
3. Vérifie le résultat dans TimeTree, puis supprime le calendrier provisoire de l'iPhone.

Attention, d'après l'aide de TimeTree : la copie est à sens unique, et chaque nouvel import crée des doublons. Ne le fais qu'une fois.

Envoyé par La Régie, à ta demande."""


def main(argv):
    if len(argv) < 3:
        print("Usage : timetree_envoi.py fichier.ics adresse@boite")
        return 1
    chemin, adresse = argv[1], argv[2].strip().lower()
    donnees = open(chemin, "rb").read()
    n = len(re.findall(rb"^BEGIN:VEVENT", donnees, re.M))
    if n == 0:
        print("Export vide : aucun mail envoyé.")
        return 1

    compte = next((a for a in send.load_accounts() if (a.get("email") or "").lower() == adresse), None)
    if not compte:
        print("Boîte %s absente des comptes du collecteur : aucun mail envoyé." % adresse)
        return 1

    msg = send.build_message(compte, compte["email"],
                             "Export TimeTree « D'clik agency » (%d événements)" % n,
                             CORPS.format(n=n), None)
    msg.add_attachment(donnees, maintype="text", subtype="calendar", filename="dclik-agency.ics")
    ok, detail = send.expedier(compte, msg)
    print("Mail envoyé à %s : %d événement(s) · %s" % (compte["email"], n, detail))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
