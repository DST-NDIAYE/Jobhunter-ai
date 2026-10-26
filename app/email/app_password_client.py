"""Client Gmail léger et fiable utilisant un Mot de Passe d'Application Google (IMAP/SMTP standard)."""

import email
from email.message import EmailMessage
import imaplib
import logging
import smtplib
import time

LOGGER = logging.getLogger(__name__)


class GmailAppPasswordError(Exception):
    pass


class AppPasswordGmail:
    """Gère la création de brouillons et l'envoi direct via Gmail avec mot de passe d'application."""

    def __init__(self, user: str, app_password: str, timeout: float = 20.0):
        self.user = user.strip()
        self.password = app_password.replace(" ", "").strip()
        self.timeout = timeout
        if not self.user or "@" not in self.user:
            raise GmailAppPasswordError("Adresse email Gmail invalide.")
        if not self.password:
            raise GmailAppPasswordError("Mot de passe d'application Google manquant.")

    def test_connection(self) -> str:
        """Vérifie la validité des identifiants auprès des serveurs Gmail."""
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=self.timeout) as server:
                server.starttls()
                server.login(self.user, self.password)
            return self.user
        except smtplib.SMTPAuthenticationError:
            raise GmailAppPasswordError(
                "Identifiants Gmail refusés. Générez un mot de passe d'application à 16 lettres sur https://myaccount.google.com/apppasswords"
            ) from None
        except Exception as exc:
            LOGGER.warning("Gmail test connection failed: %s", exc)
            raise GmailAppPasswordError(f"Connexion au serveur Gmail impossible : {exc}") from None

    def create_draft(self, email_message: EmailMessage) -> str:
        """Dépose le message directement dans la boîte 'Brouillons' de Gmail avec le CV attaché."""
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self.user, self.password)

            target_folder = None
            for folder in ('"[Gmail]/Brouillons"', '"[Gmail]/Drafts"', 'Drafts'):
                status, _ = mail.select(folder)
                if status == "OK":
                    target_folder = folder
                    break

            if not target_folder:
                target_folder = '"[Gmail]/Brouillons"'

            raw_bytes = email_message.as_bytes()
            status, _ = mail.append(target_folder, "\\Draft", imaplib.Time2Internaldate(time.time()), raw_bytes)
            if status != "OK":
                raise GmailAppPasswordError("Gmail n'a pas accepté l'enregistrement du brouillon.")

            return f"draft-imap-{int(time.time())}"
        except imaplib.IMAP4.error as exc:
            raise GmailAppPasswordError(f"Erreur d'accès aux brouillons Gmail : {exc}") from None
        except Exception as exc:
            LOGGER.warning("Gmail create draft failed: %s", exc)
            raise GmailAppPasswordError(f"Impossible de créer le brouillon dans Gmail : {exc}") from None
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def send_email(self, email_message: EmailMessage) -> str:
        """Envoie directement l'email de candidature avec le CV en pièce jointe."""
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=self.timeout) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(email_message)
            return f"sent-smtp-{int(time.time())}"
        except smtplib.SMTPAuthenticationError:
            raise GmailAppPasswordError("Authentification SMTP refusée par Gmail.") from None
        except Exception as exc:
            LOGGER.warning("Gmail send email failed: %s", exc)
            raise GmailAppPasswordError(f"Échec de l'envoi de l'email : {exc}") from None
