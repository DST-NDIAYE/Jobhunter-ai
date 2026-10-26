"""Lecture ponctuelle de trois pages publiques maximum, robots respecté, sans navigateur."""

from html.parser import HTMLParser
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import httpx

from app.application_channels import safe_url, public_email

USER_AGENT = "JobHunterAI-Contact/1.0"


def validate_public_target(url):
    if not safe_url(url):
        raise ValueError("URL publique HTTP(S) attendue.")
    parsed = urlsplit(url)
    if parsed.port not in (None, 80, 443):
        raise ValueError("Port non standard : intervention manuelle requise.")
    for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise ValueError("Les adresses réseau privées ne sont pas consultées.")


class PageContacts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.texts, self.emails = [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "head", "template"):
            self.hidden += 1
        if tag == "a" and not self.hidden:
            href = attrs.get("href", "")
            self.links.append(href)
            if href.startswith("mailto:"):
                self.emails.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "template"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.texts.append(data)


def find_public_contact(website):
    validate_public_target(website)
    root = urlsplit(website)
    base = f"{root.scheme}://{root.netloc}"
    def fetch(client, url):
        current_url = url
        for _ in range(4):
            validate_public_target(current_url)
            with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    loc = response.headers.get("Location", "")
                    if not loc:
                        raise ValueError("Redirection invalide.")
                    current_url = urljoin(current_url, loc)
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 750000:
                        raise ValueError("Page trop volumineuse : recherche de contact interrompue.")
                return bytes(content).decode("utf-8", errors="replace")
        raise ValueError("Trop de redirections sur le site.")
    try:
        with httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            robots_url = base + "/robots.txt"
            try:
                robots_text = fetch(client, robots_url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                robots_text = "User-agent: *\nAllow: /"
            rules = RobotFileParser(); rules.parse(robots_text.splitlines())
            delay = rules.crawl_delay(USER_AGENT) or 1
            if delay > 5:
                raise ValueError("Le site demande un délai élevé. Consultez ses coordonnées manuellement.")
            pending, visited, candidates = [website], set(), {}
            while pending and len(visited) < 3:
                url = pending.pop(0)
                if url in visited or not rules.can_fetch(USER_AGENT, url):
                    continue
                visited.add(url)
                time.sleep(delay)
                html = fetch(client, url)
                if any(marker in html.lower() for marker in ("captcha", "cf-chl-", "verify you are human")):
                    raise ValueError("Protection détectée. Recherche interrompue ; intervention requise.")
                parser = PageContacts(); parser.feed(html)
                text = " ".join(parser.texts)
                found = parser.emails + re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", text)
                for value in found:
                    email = public_email(value)
                    if email and not any(term in email.lower() for term in ("noreply", "no-reply", "dpo@", "privacy@")):
                        candidates[email] = url
                for href in parser.links:
                    target = urljoin(url, href)
                    if safe_url(target) and urlsplit(target).netloc == root.netloc and any(
                        term in target.lower() for term in ("carriere", "recrut", "rejoindre", "contact", "jobs", "careers")
                    ):
                        pending.append(target)
            return [{"email": email, "source_url": url} for email, url in candidates.items()]
    except (httpx.HTTPError, OSError):
        raise ValueError("Le site ne permet pas la consultation des coordonnées pour le moment.") from None
