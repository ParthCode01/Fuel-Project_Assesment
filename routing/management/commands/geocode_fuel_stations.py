import csv
import math
import os
import tempfile
import time
from pathlib import Path

import requests
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
            if (
                not key
                or not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                raise CommandError(
                    f"Station coordinate cache has invalid data on row {row_number}."
                )
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
            "--station-ids",
            help="Comma-separated station IDs to geocode instead of processing the full dataset.",
        )
        parser.add_argument(
            "--city-centroid",
            action="store_true",
            help=(
                "For selected station IDs, geocode the city and state only. "
                "This gives approximate city coordinates, not exact station coordinates."
            ),
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-geocode selected station IDs even if they are already cached.",
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

        stations = get_fuel_stations()
        locations_by_key = {}
        available_station_ids = set()
        for station in stations:
            available_station_ids.add(station.station_id)
            key = station_location_key(station)
            locations_by_key.setdefault(key, []).append(station)

        coordinates = _read_coordinate_cache()
        stations_by_location = {
            key: location_stations[0]
            for key, location_stations in locations_by_key.items()
        }
        station_ids = options["station_ids"]
        if options["city_centroid"] and not station_ids:
            raise CommandError("--city-centroid requires --station-ids.")
        if options["refresh"] and not station_ids:
            raise CommandError("--refresh requires --station-ids.")

        if station_ids:
            requested_station_ids = {
                station_id.strip()
                for station_id in station_ids.split(",")
                if station_id.strip()
            }
            unknown_station_ids = requested_station_ids - available_station_ids
            if unknown_station_ids:
                unknown = ", ".join(sorted(unknown_station_ids))
                raise CommandError(f"Unknown station IDs: {unknown}")

            stations_by_location = {}
            for key, location_stations in locations_by_key.items():
                selected_station = next(
                    (
                        station
                        for station in location_stations
                        if station.station_id in requested_station_ids
                    ),
                    None,
                )
                if selected_station is not None:
                    stations_by_location[key] = selected_station

        pending = [
            (key, station)
            for key, station in stations_by_location.items()
            if options["refresh"] or key not in coordinates
        ]
        if limit is not None:
            pending = pending[:limit]

        total_locations = len(locations_by_key)
        cached_locations = len(set(locations_by_key) & set(coordinates))
        if options["dry_run"]:
            self.stdout.write(
                f"{len(stations)} station records; {total_locations} unique locations; "
                f"{cached_locations} cached; {len(pending)} would be processed; "
                f"{total_locations - cached_locations} remaining."
            )
            return

        completed = 0
        failures = 0
        for index, (key, station) in enumerate(pending):
            if index and delay:
                time.sleep(delay)

            if options["city_centroid"]:
                query = f"{station.city}, {station.state}, USA"
            else:
                query = f"{station.address}, {station.city}, {station.state}, USA"
            try:
                coordinates[key] = geocode(query)
            except ValueError:
                failures += 1
                self.stderr.write(f"Location not found; skipped: {query}")
                continue
            except (RuntimeError, requests.RequestException) as exc:
                _write_coordinate_cache(coordinates)
                cached_locations = len(set(locations_by_key) & set(coordinates))
                self.stderr.write(
                    f"Progress: {len(stations)} records; {total_locations} unique; "
                    f"{cached_locations} cached; {completed} newly processed; "
                    f"{failures + 1} failures; "
                    f"{total_locations - cached_locations} remaining."
                )
                raise CommandError(
                    f"Geocoding stopped after {completed} new locations: {exc}"
                ) from exc

            _write_coordinate_cache(coordinates)
            completed += 1

        cached_locations = len(set(locations_by_key) & set(coordinates))
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(stations)} records; {total_locations} unique locations; "
                f"{cached_locations} cached; {completed} newly processed; "
                f"{failures} failures; "
                f"{total_locations - cached_locations} remaining."
            )
        )
