"""OAuth Google officiel et REST Gmail : seules les créations de brouillons existent."""

from concurrent.futures import Future
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.compose",
          "https://www.googleapis.com/auth/userinfo.email"]
LOGGER = logging.getLogger(__name__)
_TOKEN_LOCK = threading.RLock()
_OAUTH_LOCK = threading.Lock()


class GmailError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def _save_credentials(credentials, path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            os.chmod(temporary, 0o600)
            file.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_credentials(settings):
    with _TOKEN_LOCK:
        if not settings.gmail_token_file.is_file():
            raise GmailError("Gmail non connecté. Ouvrez Paramètres → Gmail pour vous connecter.")
        try:
            credentials = Credentials.from_authorized_user_file(str(settings.gmail_token_file))
            if not credentials.has_scopes(SCOPES):
                raise GmailError("Les autorisations Gmail sont incomplètes. Reconnectez Gmail dans Paramètres.")
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise GmailError("La connexion Gmail a expiré. Reconnectez Gmail dans Paramètres.")
                request = Request()
                def bounded_request(*args, **kwargs):
                    kwargs["timeout"] = min(settings.http_timeout, 30)
                    return request(*args, **kwargs)
                credentials.refresh(bounded_request)
                _save_credentials(credentials, settings.gmail_token_file)
            return credentials
        except GmailError:
            raise
        except Exception as exc:
            LOGGER.warning("Gmail credentials: %s", type(exc).__name__)
            raise GmailError("Connexion Gmail expirée ou indisponible. Réessayez ou reconnectez Gmail dans Paramètres.") from None


class GmailClient:
    def __init__(self, settings):
        self.settings = settings
        self.credentials = load_credentials(settings)

    def _request(self, method, url, *, body=None, creation=False):
        try:
            with httpx.Client(timeout=min(self.settings.http_timeout, 60), follow_redirects=False) as client:
                response = client.request(method, url, headers={"Authorization": "Bearer " + self.credentials.token}, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            LOGGER.warning("Gmail API: HTTP %s", status)
            if status in (401, 403):
                raise GmailError("Accès Gmail refusé. Vérifiez l'API activée et reconnectez Gmail dans Paramètres.") from None
            raise GmailError("L'API Gmail est indisponible. Réessayez plus tard.", uncertain=creation and status >= 500) from None
        except Exception as exc:
            LOGGER.warning("Gmail API: %s", type(exc).__name__)
            raise GmailError("Connexion à Gmail interrompue. Vérifiez votre connexion réseau.", uncertain=creation) from None

    def connected_email(self):
        result = self._request("GET", "https://www.googleapis.com/oauth2/v2/userinfo")
        from app.email.message_builder import validate_email
        try:
            return validate_email(result["email"])
        except (KeyError, TypeError, ValueError):
            raise GmailError("Impossible d'identifier le compte Google connecté.") from None

    def create_draft(self, raw_message):
        result = self._request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                               body={"message": {"raw": raw_message}}, creation=True)
        try:
            draft_id, message_id = result["id"], result["message"]["id"]
            if not isinstance(draft_id, str) or not draft_id or not isinstance(message_id, str) or not message_id:
                raise ValueError()
            return draft_id, message_id
        except (KeyError, TypeError, ValueError):
            raise GmailError("Gmail a répondu sans identifiant exploitable. Vérifiez vos brouillons dans Gmail.", uncertain=True) from None

    def send_message(self, raw_message):
        result = self._request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                               body={"raw": raw_message}, creation=True)
        try:
            message_id = result["id"]
            if not isinstance(message_id, str) or not message_id:
                raise ValueError()
            return message_id
        except (KeyError, TypeError, ValueError):
            raise GmailError("Gmail a répondu sans identifiant de message envoyé.", uncertain=True) from None


@dataclass
class OAuthAttempt:
    url: str
    result: Future


def start_authentication(settings):
    """L'écoute OAuth reste sur cette machine ; l'interface n'attend pas le navigateur."""
    if not settings.gmail_client_secret_file.is_file():
        raise GmailError("Ajoutez le fichier OAuth Google dans secrets/client_secret.json ou configurez GMAIL_CLIENT_SECRET_FILE.")
    if not _OAUTH_LOCK.acquire(blocking=False):
        raise GmailError("Une connexion Gmail est déjà en cours. Terminez-la ou attendez trois minutes.")
    server = None
    try:
        data = json.loads(settings.gmail_client_secret_file.read_text())
        installed = data.get("installed", {})
        if installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth" or installed.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError("Client OAuth Google de bureau attendu.")
        flow = InstalledAppFlow.from_client_config(data, SCOPES, autogenerate_code_verifier=True)
        future = Future()
        callback = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Ne pas journaliser l'URL de retour, son code OAuth ni son état.

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if urlsplit(self.path).path != "/" or not query.get("state"):
                    self.send_error(400)
                    return
                callback["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write("Retour Google reçu. Revenez dans JobHunter pour vérifier la connexion.".encode())

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 180
        flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        url, state = flow.authorization_url(access_type="offline", prompt="consent")

        def finish():
            try:
                server.handle_request()
                if not callback.get("path"):
                    raise GmailError("Connexion Gmail annulée ou délai dépassé. Cliquez de nouveau sur Connecter Gmail.")
                query = parse_qs(urlsplit(callback["path"]).query)
                if query.get("state") != [state] or query.get("error"):
                    raise GmailError("Autorisation Google annulée ou invalide. Aucun compte n'a été connecté.")
                # OAuthlib exige HTTPS pour analyser le retour ; l'écoute reste en boucle locale.
                flow.fetch_token(authorization_response=f"https://127.0.0.1:{server.server_port}" + callback["path"],
                                 timeout=min(settings.http_timeout, 30))
                if not flow.credentials.has_scopes(SCOPES):
                    raise GmailError("Google n'a pas accordé les droits nécessaires aux brouillons.")
                with _TOKEN_LOCK:
                    _save_credentials(flow.credentials, settings.gmail_token_file)
                email = GmailClient(settings).connected_email()
                future.set_result((True, email))
            except GmailError as exc:
                future.set_result((False, str(exc)))
            except Exception as exc:
                LOGGER.warning("Gmail OAuth: %s", type(exc).__name__)
                future.set_result((False, "Connexion Gmail impossible. Vérifiez le fichier OAuth et les utilisateurs de test Google."))
            finally:
                server.server_close()
                _OAUTH_LOCK.release()

        threading.Thread(target=finish, daemon=True, name="gmail-oauth").start()
        return OAuthAttempt(url, future)
    except Exception as exc:
        if server:
            server.server_close()
        _OAUTH_LOCK.release()
        LOGGER.warning("Gmail OAuth setup: %s", type(exc).__name__)
        raise GmailError("Fichier OAuth illisible ou incorrect. Téléchargez un client Google de type Application de bureau.") from None
