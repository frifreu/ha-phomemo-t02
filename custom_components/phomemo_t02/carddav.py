"""Geburtstage aus iCloud-Kontakten (CardDAV) lesen – ohne HA-Abhängigkeit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
import re
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import aiohttp

_LOGGER = logging.getLogger(__name__)

ICLOUD_URL = "https://contacts.icloud.com/"
NO_YEAR = 1604  # Apple speichert Geburtstage ohne Jahr als 1604 (X-APPLE-OMIT-YEAR)

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}

PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""
PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">
<d:prop><c:addressbook-home-set/></d:prop></d:propfind>"""
PROPFIND_BOOKS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>"""
REPORT_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">
<d:prop><c:address-data/></d:prop></c:addressbook-query>"""
REPORT_MULTIGET = """<?xml version="1.0" encoding="utf-8"?>
<c:addressbook-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">
<d:prop><c:address-data/></d:prop>{hrefs}</c:addressbook-multiget>"""


class CardDavError(Exception):
    """Allgemeiner Fehler beim Abruf."""


class CardDavAuthError(CardDavError):
    """Anmeldung abgelehnt (Apple-ID oder App-Passwort falsch)."""


@dataclass(frozen=True)
class Birthday:
    name: str
    month: int
    day: int
    year: int | None  # None, wenn ohne Jahr gespeichert

    def on(self, year: int) -> date:
        """Termin im angegebenen Jahr (29. Februar -> 28. Februar in Nicht-Schaltjahren)."""
        try:
            return date(year, self.month, self.day)
        except ValueError:
            return date(year, 2, 28)

    def age_in(self, year: int) -> int | None:
        return year - self.year if self.year else None


# ------------------------------------------------------------------ vCard

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _parse_bday(value: str) -> tuple[int | None, int, int] | None:
    value = value.strip().split("T", 1)[0]
    for pattern in (r"^(\d{4})-(\d{2})-(\d{2})$", r"^(\d{4})(\d{2})(\d{2})$"):
        if m := re.match(pattern, value):
            year, month, day = (int(g) for g in m.groups())
            return (None if year == NO_YEAR else year), month, day
    if m := re.match(r"^--(\d{2})-?(\d{2})$", value):  # vCard 4: ohne Jahr
        return None, int(m.group(1)), int(m.group(2))
    return None


def parse_vcard(text: str) -> Birthday | None:
    """Liest Name und Geburtstag aus einer vCard; None ohne Geburtstag."""
    fn = n = bday = None
    for line in _unfold(text):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        prop = key.split(";", 1)[0].split(".")[-1].upper()  # "item1.BDAY" -> "BDAY"
        if prop == "FN":
            fn = _unescape(value)
        elif prop == "N":
            parts = [_unescape(p) for p in re.split(r"(?<!\\);", value)]
            n = " ".join(p for p in (parts[1:2] + parts[0:1]) if p)  # Vorname Nachname
        elif prop == "BDAY" and bday is None:
            bday = _parse_bday(value)
    if not bday:
        return None
    name = fn or n
    if not name:
        return None
    year, month, day = bday
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return Birthday(name, month, day, year)


# ---------------------------------------------------------------- CardDAV

class ICloudBirthdays:
    """Minimaler CardDAV-Client für iCloud (Basic-Auth mit App-Passwort)."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str,
                 base_url: str = ICLOUD_URL) -> None:
        self._session = session
        self._auth = aiohttp.BasicAuth(username, password)
        self._base = base_url
        self._books: list[str] | None = None

    async def _dav(self, method: str, url: str, body: str, depth: str) -> ET.Element:
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        try:
            async with self._session.request(
                method, url, data=body.encode(), headers=headers, auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status in (401, 403):
                    raise CardDavAuthError("Anmeldung bei iCloud abgelehnt")
                if resp.status != 207:
                    raise CardDavError(f"{method} {url}: HTTP {resp.status}")
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise CardDavError(f"Verbindung zu iCloud fehlgeschlagen: {err}") from err
        try:
            return ET.fromstring(text)
        except ET.ParseError as err:
            raise CardDavError(f"Ungültige Antwort von iCloud: {err}") from err

    @staticmethod
    def _href(root: ET.Element, prop: str) -> str | None:
        el = root.find(f".//{prop}/d:href", NS)
        return el.text.strip() if el is not None and el.text else None

    async def _discover(self) -> list[str]:
        root = await self._dav("PROPFIND", self._base, PROPFIND_PRINCIPAL, "0")
        principal = self._href(root, "d:current-user-principal")
        if not principal:
            raise CardDavError("Kein Benutzerkonto gefunden (current-user-principal)")
        principal = urljoin(self._base, principal)

        root = await self._dav("PROPFIND", principal, PROPFIND_HOME, "0")
        home = self._href(root, "c:addressbook-home-set")
        if not home:
            raise CardDavError("Kein Adressbuch gefunden (addressbook-home-set)")
        home = urljoin(principal, home)

        root = await self._dav("PROPFIND", home, PROPFIND_BOOKS, "1")
        books = []
        for resp in root.findall("d:response", NS):
            href = resp.findtext("d:href", namespaces=NS)
            if href and resp.find(".//d:resourcetype/c:addressbook", NS) is not None:
                books.append(urljoin(home, href.strip()))
        if not books:
            raise CardDavError("Kein Adressbuch gefunden")
        return books

    async def _cards(self, book: str) -> list[str]:
        root = await self._dav("REPORT", book, REPORT_QUERY, "1")
        cards = [el.text for el in root.iter(f"{{{NS['c']}}}address-data") if el.text]
        if cards:
            return cards
        # Rückfall: Einträge auflisten und gezielt abholen
        root = await self._dav("PROPFIND", book, PROPFIND_BOOKS, "1")
        hrefs = [h.text.strip() for h in root.findall("d:response/d:href", NS)
                 if h.text and h.text.strip().endswith(".vcf")]
        for i in range(0, len(hrefs), 100):
            chunk = "".join(f"<d:href>{h}</d:href>" for h in hrefs[i:i + 100])
            root = await self._dav("REPORT", book, REPORT_MULTIGET.format(hrefs=chunk), "1")
            cards += [el.text for el in root.iter(f"{{{NS['c']}}}address-data") if el.text]
        return cards

    async def fetch(self) -> list[Birthday]:
        if self._books is None:
            self._books = await self._discover()
        result: dict[tuple, Birthday] = {}
        total = 0
        for book in self._books:
            for card in await self._cards(book):
                total += 1
                if bday := parse_vcard(card):
                    result[(bday.name, bday.month, bday.day)] = bday
        _LOGGER.debug("%d Kontakte gelesen, %d mit Geburtstag", total, len(result))
        return sorted(result.values(), key=lambda b: (b.month, b.day, b.name))
