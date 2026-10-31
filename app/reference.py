"""Reference data used to normalise and enrich raw sightings.

The upstream feed only gives a city name, so we map each city to a UK region,
nation and coordinates. That lets us report "per region" at several levels and
lets a front end drop the results straight onto a map.
"""

import math
from dataclasses import dataclass

UNKNOWN_REGION = "Unknown"


@dataclass(frozen=True)
class City:
    name: str
    region: str
    nation: str
    lat: float
    lon: float


CITIES: dict[str, City] = {
    c.name: c
    for c in [
        City("London", "London", "England", 51.5074, -0.1278),
        City("Birmingham", "West Midlands", "England", 52.4862, -1.8904),
        City("Manchester", "North West", "England", 53.4808, -2.2426),
        City("Liverpool", "North West", "England", 53.4084, -2.9916),
        City("Leeds", "Yorkshire and the Humber", "England", 53.8008, -1.5491),
        City("Sheffield", "Yorkshire and the Humber", "England", 53.3811, -1.4701),
        City("Newcastle", "North East", "England", 54.9783, -1.6178),
        City("Nottingham", "East Midlands", "England", 52.9548, -1.1581),
        City("Leicester", "East Midlands", "England", 52.6369, -1.1398),
        City("Bristol", "South West", "England", 51.4545, -2.5879),
        City("Southampton", "South East", "England", 50.9097, -1.4044),
        City("Cardiff", "Wales", "Wales", 51.4816, -3.1791),
        City("Glasgow", "Scotland", "Scotland", 55.8642, -4.2518),
        City("Edinburgh", "Scotland", "Scotland", 55.9533, -3.1883),
    ]
}

# Common alternative spellings we fold into the canonical city name.
_CITY_ALIASES = {
    "newcastle upon tyne": "Newcastle",
    "newcastle-upon-tyne": "Newcastle",
    "greater london": "London",
    "city of london": "London",
}
_CITY_LOOKUP = {name.lower(): name for name in CITIES} | _CITY_ALIASES

SPECIES_LATIN: dict[str, str] = {
    "Southern rodent tick": "Ixodes acuminatus",
    "Marsh tick": "Ixodes apronophorus",
    "Tree-hole tick": "Ixodes arboricola",
    "Fox/badger tick": "Ixodes canisuga",
    "Passerine tick": "Dermacentor frontalis",
}
_SPECIES_LOOKUP = {name.lower(): name for name in SPECIES_LATIN}
_LATIN_LOOKUP = {latin.lower(): name for name, latin in SPECIES_LATIN.items()}


def canonical_city(raw: str) -> str:
    """Map a cleaned city string onto its canonical spelling.

    Unknown cities are kept (title-cased) rather than rejected, so new
    locations in the feed still show up; they just get region "Unknown".
    """
    return _CITY_LOOKUP.get(raw.lower(), raw.title())


def region_for(city: str) -> str:
    known = CITIES.get(city)
    return known.region if known else UNKNOWN_REGION


def nation_for(city: str) -> str:
    known = CITIES.get(city)
    return known.nation if known else UNKNOWN_REGION


def canonical_species(raw: str) -> str:
    return _SPECIES_LOOKUP.get(raw.lower(), raw)


def species_for_latin(latin: str) -> str | None:
    return _LATIN_LOOKUP.get(latin.lower())


def latin_for(species: str) -> str | None:
    return SPECIES_LATIN.get(species)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cities_within(lat: float, lon: float, radius_km: float) -> list[str]:
    return [c.name for c in CITIES.values() if haversine_km(lat, lon, c.lat, c.lon) <= radius_km]
