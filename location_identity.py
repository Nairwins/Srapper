"""Offline country identity shared by scraping and search; ambiguity stays unresolved."""
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path


def normalize(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value or '')).casefold()
                            if not unicodedata.combining(c)).split())


@lru_cache(maxsize=1)
def reference():
    data = json.loads(Path(__file__).with_name('country_reference.json').read_text(encoding='utf-8'))
    aliases, names, cities, regions, qualified = {}, {}, {}, {}, {}
    for code, country in data['countries'].items():
        for value in country['aliases']:
            key = normalize(value)
            aliases.setdefault(key, set()).add(code)
            # Codes aren't country names in an unstructured address.
            if len(key) > 3 or key in ('usa', 'uk', 'u.k.', 'uae', 'u.s.', 'u.s.a.'):
                names.setdefault(key, set()).add(code)
    for value, name in data['regions']:
        code, local = value.split('-', 1)
        qualified[normalize(value)] = {code}
        for key in (normalize(local), normalize(name)):
            regions.setdefault(key, set()).add(code)
    for code, values in data['cities'].items():
        for city in values:
            cities.setdefault(normalize(city), set()).add(code)
    return data, aliases, names, cities, regions, qualified


def country_id(value):
    """Exact country field/query lookup. No fuzzy typo or substring guessing."""
    if isinstance(value, dict):
        value = value.get('code') or value.get('name')
    return _country_id_text(value) if isinstance(value, str) else None


@lru_cache(maxsize=4096)
def _country_id_text(value):
    options = reference()[1].get(normalize(value), set())
    return next(iter(options)) if len(options) == 1 else None


def country_name(value):
    if not isinstance(value, str):
        return value
    return _country_name_text(value)


@lru_cache(maxsize=4096)
def _country_name_text(value):
    code = country_id(value)
    return reference()[0]['countries'][code]['name'] if code else value


def _parts(text):
    return [p.strip() for p in re.split(r'[,;/()]|\s+-\s+', text) if p.strip()]


@lru_cache(maxsize=8192)
def _text_countries(raw):
    text = normalize(raw)
    if not text:
        return frozenset()
    _, aliases, names, cities, regions, qualified = reference()
    parts = _parts(text)
    if not parts:
        return frozenset()
    # Fully qualified subdivision codes, e.g. US-MA, IN-KA, BR-MA.
    iso = set().union(*(qualified.get(p, set()) for p in parts))
    if iso:
        return frozenset(iso)
    named = set()
    for part in [text] + parts:
        part = re.sub(r'^(?:remote|anywhere|based)\s+(?:in\s+)?', '', part).strip(' -')
        choices = names.get(part, set())
        # Georgia is both a country and a US state: city context is required.
        if choices and not (regions.get(part, set()) - choices):
            named.update(choices)
    if named:
        return frozenset(named)
    last = re.sub(r'\s+\d{4,6}(?:-\d+)?$', '', parts[-1])
    hint = aliases.get(last, set()) | regions.get(last, set())
    direct_hint = bool(hint)
    city_options = set().union(*(cities.get(part, set()) for part in parts))
    # Address variants such as Waltham-MA or MA - Somerville.
    for component in parts:
        pieces = re.split(r'\s*-\s*|\s+(?=[a-z]{2,3}$)', component)
        if len(pieces) > 1:
            city_options.update(set().union(*(cities.get(p, set()) for p in pieces)))
            for part in (pieces[0], pieces[-1]):
                if len(part) <= 3:
                    hint.update(aliases.get(part, set()) | regions.get(part, set()))
    if len(parts) > 1 and len(parts[0]) <= 3:
        hint.update(aliases.get(parts[0], set()) | regions.get(parts[0], set()))
    if hint:
        supported = hint & city_options
        if len(supported) == 1:
            return frozenset(supported)
        if direct_hint and not city_options and len(hint) == 1:
            return frozenset(hint)
        return frozenset()
    return frozenset(city_options) if len(city_options) == 1 else frozenset()


def country_codes(locations):
    if isinstance(locations, dict):
        values = [locations.get(key) for key in ('country', 'country_code', 'countryCode', 'addressCountry') if locations.get(key)]
        codes = {country_id(value) for value in values} - {None}
        # Conflicting structured fields need source correction, not a guess.
        return codes if len(codes) == 1 else set()
    if isinstance(locations, (list, tuple)):
        return set().union(*(country_codes(loc) for loc in locations)) if locations else set()
    return set(_text_countries(locations)) if isinstance(locations, str) else set()
