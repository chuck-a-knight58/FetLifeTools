"""Geocoding and distance helpers.

FetLife exposes no coordinates, so to measure how far a member lives from a
center point we geocode their location *string* (e.g. "Carlisle, Pennsylvania")
via OpenStreetMap's Nominatim service, with an on-disk cache. Results carry a
quality flag so the caller can surface low-confidence placements:

- ``ok``         — resolved to a city/town; distance is reliable.
- ``state-only`` — only a state/region matched; distance is a centroid estimate.
- ``not-found``  — could not be geocoded at all.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional, Tuple

from curl_cffi import requests as cffi_requests

Coord = Tuple[float, float]

OK = "ok"
STATE_ONLY = "state-only"
NOT_FOUND = "not-found"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_EARTH_RADIUS = {"mi": 3958.7613, "km": 6371.0088}

# Nominatim result types that indicate region/state granularity (no city).
_REGION_TYPES = {"state", "administrative", "region", "province", "county"}


def haversine(a: Coord, b: Coord, units: str = "mi") -> float:
    """Great-circle distance between two ``(lat, lng)`` points."""
    radius = _EARTH_RADIUS.get(units, _EARTH_RADIUS["mi"])
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def parse_latlng(text: str) -> Optional[Coord]:
    """Parse a ``"lat,lng"`` literal, or return None if it isn't one."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return None


class Geocoder:
    """Nominatim geocoder with a persistent JSON cache and polite rate limiting."""

    def __init__(
        self,
        cache_path: Path | str | None = None,
        user_agent: str = "FetLifeTools",
        min_interval: float = 1.0,
        session=None,
    ):
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.session = session or cffi_requests.Session()
        self._last_request = 0.0
        self._cache: dict[str, dict] = self._load_cache()

    # ------------------------------------------------------------------ #
    # Cache
    # ------------------------------------------------------------------ #
    def _load_cache(self) -> dict[str, dict]:
        if self.cache_path and self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_bytes().decode("utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache))
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Geocoding
    # ------------------------------------------------------------------ #
    def locate(self, query: str | None) -> tuple[Optional[Coord], str]:
        """Geocode a location string → ``(coord | None, quality)``."""
        if not query or not query.strip():
            return None, NOT_FOUND
        query = query.strip()

        literal = parse_latlng(query)
        if literal:
            return literal, OK

        if query in self._cache:
            entry = self._cache[query]
            coord = (entry["lat"], entry["lng"]) if entry.get("lat") is not None else None
            return coord, entry["quality"]

        coord, quality = self._query_nominatim(query)
        # A single-token query (e.g. "Pennsylvania") is region-level at best.
        if coord is not None and "," not in query and quality == OK:
            quality = STATE_ONLY
        self._cache[query] = {
            "lat": coord[0] if coord else None,
            "lng": coord[1] if coord else None,
            "quality": quality,
        }
        self._save_cache()
        return coord, quality

    def locate_member(self, member) -> tuple[Optional[Coord], str]:
        """Retry geocoding using a member's structured ``location`` field.

        ``member.meta['location_parts']`` (city, region, country names) is set by
        the profile parser; we try "city, region" first for a precise hit, then
        fall back to the region alone (flagged ``state-only``).
        """
        parts = (getattr(member, "meta", {}) or {}).get("location_parts") or []
        names = [p for p in parts if p]
        if len(names) >= 2:  # has a city-level entry: "City, Region"
            coord, quality = self.locate(", ".join(names[:2]))
            if coord is not None:
                return coord, quality
        if names:  # region only
            coord, _ = self.locate(names[-2] if len(names) >= 2 else names[-1])
            if coord is not None:
                return coord, STATE_ONLY
        # Last resort: the plain display string.
        return self.locate(getattr(member, "location", None))

    def _query_nominatim(self, query: str) -> tuple[Optional[Coord], str]:
        self._throttle()
        try:
            resp = self.session.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=30,
            )
        except Exception:
            return None, NOT_FOUND
        if resp.status_code != 200:
            return None, NOT_FOUND
        try:
            results = resp.json()
        except ValueError:
            return None, NOT_FOUND
        if not results:
            return None, NOT_FOUND

        top = results[0]
        try:
            coord = (float(top["lat"]), float(top["lon"]))
        except (KeyError, ValueError, TypeError):
            return None, NOT_FOUND

        addrtype = (top.get("addresstype") or top.get("type") or "").lower()
        quality = STATE_ONLY if addrtype in _REGION_TYPES else OK
        return coord, quality

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
