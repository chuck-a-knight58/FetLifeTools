"""Tests for the geo helpers (offline; Nominatim HTTP is mocked)."""

import json

import pytest
import requests
import responses

from fetlife import geo
from fetlife.geo import Geocoder


def _geocoder(cache_path):
    # Inject a plain requests.Session so the `responses` mock can intercept
    # (production uses curl_cffi, which libcurl-based mocks can't patch).
    return Geocoder(cache_path=cache_path, min_interval=0.0, session=requests.Session())


def test_haversine_known_distance():
    # NYC -> LA is ~2445 miles; allow generous tolerance.
    nyc = (40.7128, -74.0060)
    la = (34.0522, -118.2437)
    miles = geo.haversine(nyc, la, "mi")
    assert 2400 < miles < 2500
    km = geo.haversine(nyc, la, "km")
    assert 3900 < km < 4000


def test_haversine_zero():
    p = (40.0, -75.0)
    assert geo.haversine(p, p) == pytest.approx(0.0, abs=1e-6)


def test_parse_latlng():
    assert geo.parse_latlng("40.759, -74.979") == (40.759, -74.979)
    assert geo.parse_latlng("not a coord") is None
    assert geo.parse_latlng("999, 999") is None


def test_locate_latlng_literal_no_network(tmp_path):
    gc = Geocoder(cache_path=tmp_path / "geo.json")
    coord, quality = gc.locate("40.759,-74.979")
    assert coord == (40.759, -74.979)
    assert quality == geo.OK


@responses.activate
def test_locate_city_ok_and_caches(tmp_path):
    responses.add(
        responses.GET, geo.NOMINATIM_URL,
        json=[{"lat": "40.2732", "lon": "-74.9776", "addresstype": "town"}],
        status=200,
    )
    cache = tmp_path / "geo.json"
    gc = _geocoder(cache)
    coord, quality = gc.locate("Washington, New Jersey")
    assert coord == (40.2732, -74.9776)
    assert quality == geo.OK
    # Cached to disk...
    assert "Washington, New Jersey" in json.loads(cache.read_text())
    # ...and a second lookup does not hit the network again.
    responses.reset()
    coord2, quality2 = gc.locate("Washington, New Jersey")
    assert coord2 == coord and quality2 == geo.OK


@responses.activate
def test_locate_state_only(tmp_path):
    responses.add(
        responses.GET, geo.NOMINATIM_URL,
        json=[{"lat": "41.2033", "lon": "-77.1945", "addresstype": "state"}],
        status=200,
    )
    gc = _geocoder(tmp_path / "geo.json")
    coord, quality = gc.locate("Pennsylvania")
    assert coord == (41.2033, -77.1945)
    assert quality == geo.STATE_ONLY


@responses.activate
def test_single_token_downgraded_to_state_only(tmp_path):
    # Even if Nominatim returns a city type, a single-token query is region-level.
    responses.add(
        responses.GET, geo.NOMINATIM_URL,
        json=[{"lat": "1.0", "lon": "2.0", "addresstype": "city"}],
        status=200,
    )
    gc = _geocoder(tmp_path / "geo.json")
    _, quality = gc.locate("Texas")
    assert quality == geo.STATE_ONLY


@responses.activate
def test_locate_not_found_caches_negative(tmp_path):
    responses.add(responses.GET, geo.NOMINATIM_URL, json=[], status=200)
    cache = tmp_path / "geo.json"
    gc = _geocoder(cache)
    coord, quality = gc.locate("Nowheresville XYZ")
    assert coord is None and quality == geo.NOT_FOUND
    assert json.loads(cache.read_text())["Nowheresville XYZ"]["quality"] == geo.NOT_FOUND


def test_locate_empty():
    gc = Geocoder(cache_path=None)
    assert gc.locate("") == (None, geo.NOT_FOUND)
    assert gc.locate(None) == (None, geo.NOT_FOUND)
