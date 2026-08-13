"""Lean DigiKey and Mouser catalog clients.

Small stdlib-only wrappers over the two distributor APIs used by the
sourcing tools (`cs parts ...`, `cs bom verify`). DigiKey uses the v4
Product Information API with a two-legged OAuth2 client-credentials token;
Mouser uses its Search API keyed by a simple API key.

Credentials are read from the environment, falling back to
`~/.circuit_synth/credentials` (dotenv format). Never commit credentials to
a repo:

  DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET   https://developer.digikey.com
  MOUSER_API_KEY                             https://www.mouser.com/api-hub
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CREDENTIALS_FILE = Path.home() / ".circuit_synth" / "credentials"

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_DETAILS_URL = "https://api.digikey.com/products/v4/search/{}/productdetails"
DIGIKEY_KEYWORD_URL = "https://api.digikey.com/products/v4/search/keyword"
MOUSER_PARTNUMBER_URL = "https://api.mouser.com/api/v1/search/partnumber"
MOUSER_KEYWORD_URL = "https://api.mouser.com/api/v1/search/keyword"

# Mouser documents 30 search calls per minute; DigiKey is far looser.
MOUSER_MIN_INTERVAL = 2.1
DIGIKEY_MIN_INTERVAL = 0.2

# Lifecycle values worth stopping for. Anything else (Active, Preliminary,
# "New at Mouser", ...) is fine, and whitelisting the bad ones keeps reports
# quiet enough to be read.
DEAD_STATUS = (
    "obsolete",
    "discontinued",
    "endoflife",
    "eol",
    "lasttimebuy",
    "notrecommended",
    "notforenewdesigns",
    "nrnd",
)


# ------------------------------------------------------------------ helpers

def load_credentials() -> None:
    """Fill os.environ from ~/.circuit_synth/credentials for unset names."""
    if not CREDENTIALS_FILE.exists():
        return
    for line in CREDENTIALS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value


def digikey_client() -> "DigiKey | None":
    load_credentials()
    cid = os.environ.get("DIGIKEY_CLIENT_ID")
    secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    try:
        return DigiKey(cid, secret)
    except urllib.error.HTTPError as err:
        print(f"DigiKey auth failed ({err.code}); check DIGIKEY_CLIENT_ID/SECRET - skipping DigiKey")
        return None


def mouser_client() -> "Mouser | None":
    load_credentials()
    key = os.environ.get("MOUSER_API_KEY")
    return Mouser(key) if key else None


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def same_maker(ours: str, theirs: str) -> bool:
    """Distributors qualify manufacturer names ("... - Diodes Division")."""
    a, b = norm(ours), norm(theirs)
    return bool(a) and bool(b) and (a in b or b in a)


def dead(status: str) -> bool:
    return any(bad in norm(status) for bad in DEAD_STATUS)


def post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_json(url: str, headers: dict) -> dict | None:
    """None when the catalog has no such part (404)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise RuntimeError(f"{err.code} {err.reason}: {err.read()[:300].decode(errors='replace')}") from err


def download(url: str, dest: Path) -> Path:
    """Fetch a file (datasheet PDF and the like) with a browser-ish UA."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) circuit-synth datasheet fetch",
            "Accept": "application/pdf,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


# ----------------------------------------------------------------- digikey

class DigiKey:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        data = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            }
        ).encode()
        req = urllib.request.Request(
            DIGIKEY_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.token = json.loads(resp.read())["access_token"]
        self.last_call = 0.0

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "X-DIGIKEY-Locale-Site": "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "USD",
        }

    def _throttle(self) -> None:
        wait = DIGIKEY_MIN_INTERVAL - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()

    def by_number(self, number: str) -> dict | None:
        """Product for a DigiKey part number or exact MPN; None when unknown."""
        self._throttle()
        payload = get_json(
            DIGIKEY_DETAILS_URL.format(urllib.parse.quote(number, safe="")), self._headers()
        )
        return (payload or {}).get("Product")

    def search(self, keywords: str, limit: int = 10, in_stock: bool = False) -> list[dict]:
        """Catalog entries for a keyword query, exact MPN matches first."""
        self._throttle()
        payload: dict = {"Keywords": keywords, "Limit": limit, "Offset": 0}
        if in_stock:
            payload["FilterOptionsRequest"] = {"MinimumQuantityAvailable": 1}
        result = post_json(DIGIKEY_KEYWORD_URL, payload, self._headers())
        products = (result.get("ExactMatches") or []) + (result.get("Products") or [])
        seen: set[str] = set()
        unique = []
        for p in products:
            key = p.get("ManufacturerProductNumber", "")
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique


def digikey_numbers(product: dict) -> dict[str, str]:
    """DigiKey product number -> package type name, per variation."""
    return {
        v.get("DigiKeyProductNumber", ""): (v.get("PackageType") or {}).get("Name", "")
        for v in product.get("ProductVariations") or []
    }


def digikey_offer(product: dict) -> str:
    """The number worth storing: cut tape preferred (no reels)."""
    numbers = digikey_numbers(product)
    cut = [num for num, pkg in numbers.items() if "cut tape" in pkg.lower()]
    return cut[0] if cut else next(iter(numbers), "")


# ------------------------------------------------------------------ mouser

class Mouser:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.last_call = 0.0

    def _throttle(self) -> None:
        wait = MOUSER_MIN_INTERVAL - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()

    def _post(self, url: str, payload: dict) -> dict:
        self._throttle()
        result = post_json(f"{url}?apiKey={urllib.parse.quote(self.api_key)}", payload, {})
        errors = result.get("Errors") or []
        if errors:
            raise RuntimeError("; ".join(str(e.get("Message", e)) for e in errors))
        return result

    def lookup(self, mpn: str) -> dict | None:
        """The catalog part whose MPN matches exactly, if any."""
        payload = self._post(
            MOUSER_PARTNUMBER_URL,
            {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}},
        )
        found = ((payload.get("SearchResults") or {}).get("Parts")) or []
        for part in found:
            if norm(part.get("ManufacturerPartNumber", "")) == norm(mpn):
                return part
        return None

    def search(self, keywords: str, limit: int = 10, in_stock: bool = False) -> list[dict]:
        payload = self._post(
            MOUSER_KEYWORD_URL,
            {
                "SearchByKeywordRequest": {
                    "keyword": keywords,
                    "records": limit,
                    "startingRecord": 0,
                    "searchOptions": "InStock" if in_stock else "",
                }
            },
        )
        return ((payload.get("SearchResults") or {}).get("Parts")) or []
