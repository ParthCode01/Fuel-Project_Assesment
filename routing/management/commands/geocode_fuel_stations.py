import csv
import os
import tempfile
import time
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services.fuel_data import (
    STATION_COORDINATES_PATH,
    get_fuel_stations,
    station_location_key,
)
from routing.services.geocoding import geocode

COORDINATE_CACHE_PATH = STATION_COORDINATES_PATH
CACHE_COLUMNS = ("location_key", "latitude", "longitude")


def _read_coordinate_cache():
    if not COORDINATE_CACHE_PATH.exists():
        return {}

    with COORDINATE_CACHE_PATH.open(newline="", encoding="utf-8") as cache_file:
        reader = csv.DictReader(cache_file)
        if not set(CACHE_COLUMNS).issubset(reader.fieldnames or []):
            raise CommandError("Station coordinate cache has invalid columns.")

        coordinates = {}
        for row_number, row in enumerate(reader, start=2):
            try:
                key = row["location_key"]
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CommandError(
                    f"Station coordinate cache has invalid data on row {row_number}."
                ) from exc
            coordinates[key] = (latitude, longitude)

    return coordinates


def _write_coordinate_cache(coordinates):
    COORDINATE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=COORDINATE_CACHE_PATH.parent,
            delete=False,
        ) as cache_file:
            temporary_path = Path(cache_file.name)
            writer = csv.DictWriter(cache_file, fieldnames=CACHE_COLUMNS)
            writer.writeheader()
            for key, (latitude, longitude) in coordinates.items():
                writer.writerow(
                    {
                        "location_key": key,
                        "latitude": latitude,
                        "longitude": longitude,
                    }
                )
        os.replace(temporary_path, COORDINATE_CACHE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


class Command(BaseCommand):
    help = "Geocode unique fuel station locations and cache their coordinates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of uncached locations to geocode in this run.",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Seconds to wait between external geocoding requests (default: 1).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the number of uncached locations without calling ORS.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delay = options["delay"]
        if limit is not None and limit < 0:
            raise CommandError("--limit must be zero or greater.")
        if delay < 0:
            raise CommandError("--delay must be zero or greater.")

        stations_by_location = {}
        for station in get_fuel_stations():
            key = station_location_key(station)
            stations_by_location.setdefault(key, station)

        coordinates = _read_coordinate_cache()
        pending = [
            (key, station)
            for key, station in stations_by_location.items()
            if key not in coordinates
        ]
        if limit is not None:
            pending = pending[:limit]

        if options["dry_run"]:
            self.stdout.write(
                f"{len(stations_by_location)} unique station locations; "
                f"{len(coordinates)} cached; {len(pending)} would be geocoded."
            )
            return

        completed = 0
        not_found = 0
        for index, (key, station) in enumerate(pending):
            if index and delay:
                time.sleep(delay)

            query = f"{station.address}, {station.city}, {station.state}, USA"
            try:
                coordinates[key] = geocode(query)
            except ValueError:
                not_found += 1
                self.stderr.write(f"Location not found; skipped: {query}")
                continue
            except (RuntimeError, requests.RequestException) as exc:
                _write_coordinate_cache(coordinates)
                raise CommandError(
                    f"Geocoding stopped after {completed} new locations: {exc}"
                ) from exc

            _write_coordinate_cache(coordinates)
            completed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Geocoded {completed} locations; "
                f"{len(coordinates)} cached; {not_found} not found."
            )
        )
