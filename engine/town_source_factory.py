#!/usr/bin/env python3
"""Town-source factory (B24, Wave B).

Generates ``source``-row dicts for the PakLead 2h web sweep from the
Wave-A harvested town/city trees (all URLs/ids taken verbatim from the
sites' own pages — nothing is guessed). The module NEVER writes to any
database and NEVER fetches the web in normal operation; it only reads
the harvested JSON trees and emits row dicts.

Integration contract (coordinator)
----------------------------------
1. ``dry_run(trees_dir, cities, platforms)`` — print the full plan
   (counts per city/platform, sample rows). Creates nothing.
2. Human reviews the plan.
3. Coordinator applies approved rows with::

       INSERT OR IGNORE INTO source
           (source_id, platform, access_route, display_name, archetype,
            city_scope, enabled, boundary_note, watermark, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, datetime('now'));

   Note: ``city_scope`` is a Python list in the row dicts produced here —
   the caller must ``json.dumps`` it before insert (the column is a JSON
   text array). Extra key ``thin_market=True`` (Quetta only) is metadata
   for the yield monitor — the ``source`` table has no such column, so the
   caller should either drop it or annotate it in ``boundary_status``/
   ``boundary_note`` instead of inserting it.

Source-id schemes (dedupe-safe, deterministic)
----------------------------------------------
* Town rows:   ``{platform}-town-{cityslug}-{townslug}``
  e.g. ``olx-town-lahore-dha-defence``, ``zameen-town-karachi-abid-town``.
* City rows (new cities only — Lahore/Karachi city pages are already
  live in the DB): ``{platform}-web-{cityslug}-houses``
  e.g. ``olx-web-faisalabad-houses``, ``zameen-web-quetta-houses``.
  This matches the existing family (``olx-web-islamabad-houses``, …).

Pagination defaults (Wave-A evidence)
--------------------------------------
* OLX: ``pages:1``. Wave-A A01: town pages carry 21 items on page 1
  (20 organic + 1 promoted pin, expected >= 20); the collector already
  stops on empty later pages.
* Zameen: ``pages:3``. Wave-A A09: pages 1..23 were deep and clean
  (25 fresh cards each, hard 404 at the boundary); 3 = freshness + buffer
  for the 2h sweep. One-time deep backfills use the ``backfill_pages``
  parameter instead (then revert to 3).

``boundary_note`` is always ``url:<url>;pages:<n>`` — the exact config
shape ``collectors/web_listings.py`` parses. Zameen URLs get
``?sort=date_desc`` appended, matching the existing live rows.

stdlib only (json, re, logging — plus argparse/pathlib for the CLI).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Constants

#: Default directory holding the Wave-A harvested tree JSONs.
DEFAULT_TREES_DIR = os.environ.get(
    "PAKLEAD_TREES_DIR",
    os.path.expanduser("~/workspace/paklead_ops/fleet"),
)

#: Cities whose town trees were harvested per-platform.
CORE_CITIES = ("Lahore", "Karachi")

#: New cities: city-level rows on both platforms + OLX town rows from
#: a05_scratch/all_towns.json (Zameen harvested no town data for them).
NEW_CITIES = (
    "Faisalabad",
    "Multan",
    "Peshawar",
    "Gujranwala",
    "Sialkot",
    "Hyderabad",
    "Quetta",
)

ALL_CITIES = CORE_CITIES + NEW_CITIES
PLATFORMS = ("olx", "zameen")

#: Verified URL patterns (Wave-A A01/A02/A03/A04/A05/A06).
OLX_TOWN_URL_RE = re.compile(
    r"^https://www\.olx\.com\.pk/[a-z0-9-]+_g\d+/houses_c1721$"
)
ZAMEEN_URL_RE = re.compile(
    r"^https://www\.zameen\.com/Houses_Property/"
    r"[A-Za-z][A-Za-z0-9_]*-\d+-1\.html(\?sort=date_desc)?$"
)
SOURCE_ID_RE = re.compile(r"^(olx|zameen)-(town|web)-[a-z0-9-]+$")
BOUNDARY_NOTE_RE = re.compile(r"^url:(https?://\S+);pages:(\d+)$")

#: Sort suffix appended to every Zameen URL (matches the live rows).
ZAMEEN_SORT_SUFFIX = "?sort=date_desc"

#: pages default for the 2h sweep.
OLX_SWEEP_PAGES = 1
ZAMEEN_SWEEP_PAGES = 3
#: Upper sanity bound for pages (A09 found a hard 404 at page 24).
MAX_SANE_PAGES = 30

REQUIRED_KEYS = (
    "source_id",
    "platform",
    "access_route",
    "display_name",
    "archetype",
    "city_scope",
    "boundary_note",
    "enabled",
)


# ----------------------------------------------------------------------
# Helpers

def city_slug(city: str) -> str:
    """Normalise a city name to the slug used in source ids."""
    return city.strip().lower().replace(" ", "-")


def normalize_city(city: str) -> str:
    """Accept a slug or display name, return the canonical display name."""
    for known in ALL_CITIES:
        if city.strip().lower() in (known.lower(), city_slug(known)):
            return known
    raise ValueError(
        f"unknown city {city!r} (known: {', '.join(ALL_CITIES)}). "
        "Islamabad's towns are already live sources in this project's DB, "
        "so the factory has nothing new to generate for it.")


def normalize_platform(platform: str) -> str:
    """Return the canonical platform name or raise."""
    p = platform.strip().lower()
    if p not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r} (known: {PLATFORMS})")
    return p


def slugify_area_token(token: str) -> str:
    """Turn a Zameen URL area token into a lowercase hyphenated slug."""
    s = token.lower().replace("_", "-").replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def zameen_area_slug(url: str, city: str) -> str:
    """Derive the town slug from a harvested Zameen URL's area token.

    E.g. ``.../Lahore_Abdalians_Cooperative_Housing_Society-492-1.html``
    -> ``abdalians-cooperative-housing-society``. The token comes from the
    site's own page, never invented.
    """
    m = re.search(r"/Houses_Property/([A-Za-z][A-Za-z0-9_]*)-(\d+)-1\.html", url)
    if not m:
        raise ValueError(f"cannot parse Zameen URL: {url!r}")
    area_city, area_token = m.group(1).split("_", 1)
    if area_city.lower() != city.lower():
        raise ValueError(
            f"Zameen URL city token {area_city!r} != expected {city!r}: {url!r}"
        )
    return slugify_area_token(area_token)


# ----------------------------------------------------------------------
# Tree loaders (read-only)

def _read_json(path: pathlib.Path) -> object:
    """Read a JSON file; raise a clear error if it is missing.

    The harvested tree JSONs are operator data, not repo data — a fresh
    clone does not have them. Point at your own with the
    PAKLEAD_TREES_DIR env var, or write source rows by hand with the
    boundary_note format from the README's worked web_public example.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"harvested tree not found: {path}\n"
            "This file is operator data (harvested from the sites' own "
            "link trees), not repo data. Either set PAKLEAD_TREES_DIR to "
            "the directory holding your tree JSONs, or create source rows "
            "manually with the boundary_note format from the README's "
            "worked web_public sweep example:\n"
            '  url:https://www.olx.com.pk/g-13_g5001007/houses_c1721;pages:1')
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_olx_towns(trees_dir: str | os.PathLike, city: str) -> list[dict]:
    """Load the OLX town tree for a core city (``olx_<slug>_towns.json``)."""
    city = normalize_city(city)
    path = pathlib.Path(trees_dir) / f"olx_{city_slug(city)}_towns.json"
    rows = _read_json(path)
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list, got {type(rows).__name__}")
    return rows


def load_zameen_towns(trees_dir: str | os.PathLike, city: str) -> list[dict]:
    """Load the Zameen town tree for a core city (``zameen_<slug>_towns.json``)."""
    city = normalize_city(city)
    path = pathlib.Path(trees_dir) / f"zameen_{city_slug(city)}_towns.json"
    rows = _read_json(path)
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list, got {type(rows).__name__}")
    return rows


def load_olx_new_cities(trees_dir: str | os.PathLike) -> list[dict]:
    """Load ``olx_new_cities.json`` (city-level verified OLX rows)."""
    doc = _read_json(pathlib.Path(trees_dir) / "olx_new_cities.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("cities"), list):
        raise ValueError("olx_new_cities.json: expected {'cities': [...]}")
    return doc["cities"]


def load_zameen_new_cities(trees_dir: str | os.PathLike) -> list[dict]:
    """Load ``zameen_new_cities.json`` ``verified_cities`` (city-level rows)."""
    doc = _read_json(pathlib.Path(trees_dir) / "zameen_new_cities.json")
    cities = doc.get("verified_cities") if isinstance(doc, dict) else None
    if not isinstance(cities, list):
        raise ValueError("zameen_new_cities.json: expected 'verified_cities' list")
    return cities


def load_new_city_town_map(
    trees_dir: str | os.PathLike, city: str
) -> dict[str, str]:
    """Load the per-city OLX town map from ``a05_scratch/all_towns.json``.

    Returns ``{slug_gid: town_name}`` e.g.
    ``{"city-housing-society_g8358": "City Housing Society"}``. The
    ``slug_gid`` keys come verbatim from the site's own pages; the factory
    reconstructs the URL with the verified OLX pattern
    ``{slug}_g{id}/houses_c1721`` — the id itself is never guessed.
    """
    city = normalize_city(city)
    path = pathlib.Path(trees_dir) / "a05_scratch" / "all_towns.json"
    doc = _read_json(path)
    if not isinstance(doc, dict) or city not in doc:
        raise ValueError(f"{path}: no town map for city {city!r}")
    towns = doc[city]
    if not isinstance(towns, dict):
        raise ValueError(f"{path}: {city} town map is not an object")
    return towns


# ----------------------------------------------------------------------
# Row generation

def _town_row(
    platform: str,
    city: str,
    townslug: str,
    url: str,
    pages: int,
) -> dict:
    """Build one source-row dict for a harvested town/city URL."""
    row = {
        "source_id": f"{platform}-town-{city_slug(city)}-{townslug}",
        "platform": platform,
        "access_route": "web_public",
        "display_name": f"{platform}-town-{city_slug(city)}-{townslug}",
        "archetype": "property",
        "city_scope": [city],
        "boundary_note": f"url:{url};pages:{pages}",
        "enabled": 1,
    }
    if city == "Quetta":
        # Verified thin market (Zameen Quetta: 5 listings) — flag so the
        # yield monitor does not raise zero-yield alarms on these rows.
        row["thin_market"] = True
    return row


def generate(
    trees_dir: str | os.PathLike,
    city: str,
    platform: str,
    backfill_pages: int | None = None,
) -> list[dict]:
    """Generate source-row dicts for one city + platform from harvested trees.

    :param trees_dir: directory holding the Wave-A tree JSONs.
    :param city: city name or slug (e.g. ``"Lahore"`` / ``"lahore"``).
    :param platform: ``"olx"`` or ``"zameen"``.
    :param backfill_pages: optional override of the Zameen pages value for
        a one-time deep backfill (A09 evidence). ``None`` -> 3 (sweep
        default). OLX always gets ``pages:1``.
    :returns: list of row dicts (pure data — no DB touched).
    """
    city = normalize_city(city)
    platform = normalize_platform(platform)
    zpages = ZAMEEN_SWEEP_PAGES if backfill_pages is None else int(backfill_pages)
    if zpages < 1:
        raise ValueError("backfill_pages must be >= 1")
    rows: list[dict] = []

    if city in CORE_CITIES:
        if platform == "olx":
            for town in load_olx_towns(trees_dir, city):
                rows.append(
                    _town_row(platform, city, town["town_slug"],
                              town["url"], OLX_SWEEP_PAGES)
                )
        else:
            for area in load_zameen_towns(trees_dir, city):
                url = area["url"] + ZAMEEN_SORT_SUFFIX
                rows.append(
                    _town_row(platform, city,
                              zameen_area_slug(area["url"], city), url, zpages)
                )
    else:
        # New cities: city-level row + (OLX only) per-town rows.
        if platform == "olx":
            found = None
            for entry in load_olx_new_cities(trees_dir):
                if normalize_city(entry["city"]) == city:
                    found = entry
                    break
            if found is None:
                raise ValueError(f"no OLX city entry for {city!r}")
            rows.append(
                {
                    "source_id": f"olx-web-{city_slug(city)}-houses",
                    "platform": platform,
                    "access_route": "web_public",
                    "display_name": f"olx-web-{city_slug(city)}-houses",
                    "archetype": "property",
                    "city_scope": [city],
                    "boundary_note": (
                        f"url:{found['houses_url']};pages:{OLX_SWEEP_PAGES}"
                    ),
                    "enabled": 1,
                    **({"thin_market": True} if city == "Quetta" else {}),
                }
            )
            for slug_gid, _name in load_new_city_town_map(trees_dir, city).items():
                if not re.fullmatch(r"[a-z0-9-]+_g\d+", slug_gid):
                    raise ValueError(
                        f"a05 town entry {slug_gid!r} does not match the "
                        "verified OLX slug_gid shape — refusing to invent a URL"
                    )
                townslug = slug_gid.rsplit("_g", 1)[0]
                url = f"https://www.olx.com.pk/{slug_gid}/houses_c1721"
                rows.append(
                    _town_row(platform, city, townslug, url, OLX_SWEEP_PAGES)
                )
        else:
            found = None
            for entry in load_zameen_new_cities(trees_dir):
                if normalize_city(entry["city"]) == city:
                    found = entry
                    break
            if found is None:
                raise ValueError(f"no Zameen city entry for {city!r}")
            rows.append(
                {
                    "source_id": f"zameen-web-{city_slug(city)}-houses",
                    "platform": platform,
                    "access_route": "web_public",
                    "display_name": f"zameen-web-{city_slug(city)}-houses",
                    "archetype": "property",
                    "city_scope": [city],
                    "boundary_note": (
                        f"url:{found['houses_url']}{ZAMEEN_SORT_SUFFIX}"
                        f";pages:{zpages}"
                    ),
                    "enabled": 1,
                    **({"thin_market": True} if city == "Quetta" else {}),
                }
            )
    return rows


def generate_all(
    trees_dir: str | os.PathLike | None = None,
    cities: list[str] | None = None,
    platforms: list[str] | None = None,
    backfill_pages: int | None = None,
) -> dict[tuple[str, str], list[dict]]:
    """Generate rows for every requested (city, platform) pair.

    Returns a dict keyed by ``(city, platform)``. No DB touched.
    ``trees_dir`` defaults to :data:`DEFAULT_TREES_DIR`.
    """
    trees_dir = trees_dir or DEFAULT_TREES_DIR
    cities = [normalize_city(c) for c in (cities or list(ALL_CITIES))]
    platforms = [normalize_platform(p) for p in (platforms or list(PLATFORMS))]
    out: dict[tuple[str, str], list[dict]] = {}
    for city in cities:
        for platform in platforms:
            out[(city, platform)] = generate(
                trees_dir, city, platform, backfill_pages=backfill_pages
            )
    return out


# ----------------------------------------------------------------------
# Validation

def _known_urls(
    trees_dir: str | os.PathLike,
    cities: list[str],
) -> dict[str, tuple[str, str]]:
    """Build the map of every URL traceable to the harvested trees.

    Returns ``{url: (city, platform)}``. Zameen URLs are stored WITHOUT
    the ``?sort=date_desc`` suffix the factory appends (stripped before
    comparison). OLX new-city town URLs are reconstructed from the
    harvested ``slug_gid`` keys with the verified pattern — each id still
    traces 1:1 to a harvested entry, never invented.
    """
    known: dict[str, tuple[str, str]] = {}
    for city in cities:
        if city in CORE_CITIES:
            for town in load_olx_towns(trees_dir, city):
                known[town["url"]] = (city, "olx")
            for area in load_zameen_towns(trees_dir, city):
                known[area["url"]] = (city, "zameen")
        else:
            for entry in load_olx_new_cities(trees_dir):
                if normalize_city(entry["city"]) == city:
                    known[entry["houses_url"]] = (city, "olx")
            for slug_gid in load_new_city_town_map(trees_dir, city):
                known[f"https://www.olx.com.pk/{slug_gid}/houses_c1721"] = (
                    city, "olx",
                )
            for entry in load_zameen_new_cities(trees_dir):
                if normalize_city(entry["city"]) == city:
                    known[entry["houses_url"]] = (city, "zameen")
    return known


def _expected_count(trees_dir, city: str, platform: str) -> int:
    """How many rows the trees say one (city, platform) pair should yield."""
    if city in CORE_CITIES:
        if platform == "olx":
            return len(load_olx_towns(trees_dir, city))
        return len(load_zameen_towns(trees_dir, city))
    count = 1  # the city-level row
    if platform == "olx":
        count += len(load_new_city_town_map(trees_dir, city))
    return count


def _parse_boundary_note(note: str) -> tuple[str | None, int | None]:
    """Split ``url:<url>;pages:<n>``; returns (url, pages) or (None, None)."""
    m = BOUNDARY_NOTE_RE.match(note or "")
    if not m:
        return None, None
    try:
        return m.group(1), int(m.group(2))
    except ValueError:
        return None, None


def validate(
    rows: list[dict],
    trees_dir: str | os.PathLike | None = None,
    cities: list[str] | None = None,
    platforms: list[str] | None = None,
) -> list[str]:
    """Validate generated row dicts. Returns a list of error strings (empty
    = all checks passed).

    Checks: required keys; value enums (platform/access_route/archetype/
    enabled); source_id shape; boundary_note shape (``url:…;pages:…``);
    URL shape matches the verified platform patterns; pages in range
    (OLX must be exactly 1); no duplicate source_id or URL; city_scope
    agrees with the source_id; thin_market flag present iff city is Quetta.

    When ``trees_dir`` is given, two extra checks run: every URL traces to
    a harvested tree entry (no guessed ids), and every harvested town has
    exactly one row (coverage, scoped to ``cities``/``platforms`` — default:
    the cities/platforms present in ``rows``).
    """
    errors: list[str] = []
    seen_ids: dict[str, int] = {}
    seen_urls: dict[str, int] = {}

    for i, row in enumerate(rows):
        where = f"row[{i}]"
        missing = [k for k in REQUIRED_KEYS if k not in row]
        if missing:
            errors.append(f"{where}: missing keys {missing}")
            continue
        sid = row["source_id"]
        if not isinstance(sid, str) or not SOURCE_ID_RE.match(sid):
            errors.append(f"{where}: bad source_id {sid!r}")
            continue
        _kind = sid.split("-")[1]
        platform = row["platform"]
        if platform not in PLATFORMS:
            errors.append(f"{where} {sid}: bad platform {platform!r}")
        elif not sid.startswith(platform + "-"):
            errors.append(f"{where}: source_id {sid!r} != platform {platform!r}")
        if row["access_route"] != "web_public":
            errors.append(f"{where} {sid}: bad access_route {row['access_route']!r}")
        if row["archetype"] != "property":
            errors.append(f"{where} {sid}: bad archetype {row['archetype']!r}")
        if row["enabled"] != 1:
            errors.append(f"{where} {sid}: enabled must be 1, got {row['enabled']!r}")
        cs = row["city_scope"]
        if not (isinstance(cs, list) and cs and all(isinstance(c, str) for c in cs)):
            errors.append(f"{where} {sid}: city_scope must be a non-empty list of str")
        else:
            exp_slug = city_slug(cs[0])
            parts = sid.split("-")
            # town rows: platform-town-cityslug-townslug ; web rows: platform-web-cityslug-houses
            sid_city = parts[2] if parts[1] in ("town", "web") else None
            if sid_city != exp_slug:
                errors.append(
                    f"{where} {sid}: city_scope {cs} disagrees with source_id"
                )
            thin = row.get("thin_market", False)
            if (cs[0] == "Quetta") != (thin is True):
                errors.append(
                    f"{where} {sid}: thin_market must be True iff city is Quetta"
                )
        url, pages = _parse_boundary_note(row.get("boundary_note", ""))
        if url is None:
            errors.append(f"{where} {sid}: bad boundary_note {row['boundary_note']!r}")
            continue
        pattern = OLX_TOWN_URL_RE if platform == "olx" else ZAMEEN_URL_RE
        if not pattern.match(url):
            errors.append(f"{where} {sid}: URL fails {platform} pattern: {url!r}")
        if pages is None or pages < 1:
            errors.append(f"{where} {sid}: pages must be >= 1, got {pages!r}")
        elif pages > MAX_SANE_PAGES:
            errors.append(f"{where} {sid}: pages {pages} exceeds sane bound {MAX_SANE_PAGES}")
        elif platform == "olx" and pages != OLX_SWEEP_PAGES:
            errors.append(f"{where} {sid}: OLX pages must be {OLX_SWEEP_PAGES}, got {pages}")
        if sid in seen_ids:
            errors.append(f"{where}: duplicate source_id {sid!r} (also row[{seen_ids[sid]}])")
        else:
            seen_ids[sid] = i
        if url in seen_urls:
            errors.append(f"{where}: duplicate URL {url!r} (also row[{seen_urls[url]}])")
        else:
            seen_urls[url] = i

    if trees_dir is not None:
        if cities is None:
            cities = sorted({r["city_scope"][0] for r in rows
                             if isinstance(r.get("city_scope"), list) and r["city_scope"]})
        if platforms is None:
            platforms = sorted({r["platform"] for r in rows
                                if r.get("platform") in PLATFORMS})
        known = _known_urls(trees_dir, cities)
        for i, row in enumerate(rows):
            url, _ = _parse_boundary_note(row.get("boundary_note", ""))
            if url is None:
                continue
            traced = url
            if row.get("platform") == "zameen":
                traced = url.removesuffix(ZAMEEN_SORT_SUFFIX)
            hit = known.get(traced)
            if hit is None:
                errors.append(
                    f"row[{i}] {row.get('source_id')}: URL does not trace to any "
                    f"harvested tree entry: {traced!r}"
                )
            elif hit[0] != row["city_scope"][0] or hit[1] != row["platform"]:
                errors.append(
                    f"row[{i}] {row.get('source_id')}: traced to {hit} but row says "
                    f"({row['city_scope'][0]}, {row['platform']})"
                )
        # Coverage: every harvested town must have exactly one row in scope.
        expected: dict[tuple[str, str], int] = {}
        for city in cities:
            for platform in platforms:
                expected[(city, platform)] = _expected_count(trees_dir, city, platform)
        actual: dict[tuple[str, str], int] = {}
        for row in rows:
            if isinstance(row.get("city_scope"), list) and row["city_scope"]:
                key = (row["city_scope"][0], row.get("platform"))
                actual[key] = actual.get(key, 0) + 1
        for key, want in expected.items():
            got = actual.get(key, 0)
            if got != want:
                errors.append(
                    f"coverage {key[0]}/{key[1]}: expected {want} rows from trees, "
                    f"got {got}"
                )
    return errors


# ----------------------------------------------------------------------
# Dry run

def dry_run(
    trees_dir: str | os.PathLike | None = None,
    cities: list[str] | None = None,
    platforms: list[str] | None = None,
    backfill_pages: int | None = None,
) -> tuple[list[dict], list[str]]:
    """Print the full generation plan WITHOUT creating anything.

    Prints per city/platform counts, sample source ids, thin-market flags
    and validation results. Returns ``(rows, errors)`` for the caller.
    """
    trees_dir = trees_dir or DEFAULT_TREES_DIR
    groups = generate_all(trees_dir, cities, platforms, backfill_pages)
    rows = [r for group in groups.values() for r in group]
    errors = validate(rows, trees_dir=trees_dir)

    print("PakLead town-source factory — DRY RUN (creates nothing)")
    print(f"trees: {trees_dir}")
    if backfill_pages is not None:
        print(f"backfill_pages override: {backfill_pages} (Zameen rows only)")
    print()
    total = 0
    for (city, platform), group in sorted(groups.items()):
        thin = sum(1 for r in group if r.get("thin_market"))
        total += len(group)
        sample = ", ".join(r["source_id"] for r in group[:2])
        print(f"  {city}/{platform}: {len(group)} rows"
              + (f" (thin_market: {thin})" if thin else "")
              + f"  e.g. {sample}")
    print()
    print(f"TOTAL: {total} rows")
    if errors:
        print(f"VALIDATION ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  ! {e}")
    else:
        print("validate(): 0 errors — all rows trace to harvested trees.")
    return rows, errors


# ----------------------------------------------------------------------
# CLI — default entry behaviour is the dry run.

def main(argv: list[str] | None = None) -> int:
    """CLI. Default (no flags): dry_run over all cities and platforms."""
    ap = argparse.ArgumentParser(
        description="Generate PakLead town source rows (dry run by default)."
    )
    ap.add_argument("--trees-dir", default=None,
                    help="directory of Wave-A tree JSONs")
    ap.add_argument("--city", action="append", default=None,
                    help="city to include (repeatable; default: all)")
    ap.add_argument("--platform", action="append", default=None,
                    help="platform to include: olx|zameen (repeatable)")
    ap.add_argument("--backfill-pages", type=int, default=None,
                    help="one-time Zameen deep-backfill page count")
    ap.add_argument("--emit-json", action="store_true",
                    help="emit generated rows as JSON on stdout instead of "
                         "the human-readable plan (for coordinator piping)")
    args = ap.parse_args(argv)

    if args.emit_json:
        groups = generate_all(args.trees_dir or DEFAULT_TREES_DIR,
                              args.city, args.platform, args.backfill_pages)
        rows = [r for group in groups.values() for r in group]
        errs = validate(rows, trees_dir=args.trees_dir or DEFAULT_TREES_DIR)
        if errs:
            for e in errs:
                print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0

    _, errors = dry_run(args.trees_dir or DEFAULT_TREES_DIR,
                        args.city, args.platform, args.backfill_pages)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
