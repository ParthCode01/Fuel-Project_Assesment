import csv
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from routing.services import fuel_data
from routing.services.fuel_data import FuelStation, get_fuel_stations
from routing.services.geocoding import geocode
from routing.services.routing import get_route
from routing.management.commands import geocode_fuel_stations


class FuelDataTests(TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.csv_path = Path(self.temp_directory.name) / "fuel-prices.csv"
        self.csv_path.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            " 12 , TEST STATION , I-10 EXIT 1 , Phoenix , AZ , 123 ,3.499\n",
            encoding="utf-8",
        )
        self.path_patcher = patch.object(fuel_data, "FUEL_DATA_PATH", self.csv_path)
        self.path_patcher.start()
        self.addCleanup(self.path_patcher.stop)
        get_fuel_stations.cache_clear()
        self.addCleanup(get_fuel_stations.cache_clear)

    def test_loads_and_normalizes_fuel_station_rows(self):
        self.assertEqual(
            get_fuel_stations(),
            (
                FuelStation(
                    station_id="12",
                    name="TEST STATION",
                    address="I-10 EXIT 1",
                    city="Phoenix",
                    state="AZ",
                    rack_id="123",
                    price=3.499,
                ),
            ),
        )

    def test_caches_loaded_rows(self):
        first_load = get_fuel_stations()
        self.csv_path.write_text("", encoding="utf-8")

        self.assertIs(get_fuel_stations(), first_load)
        self.assertEqual(len(get_fuel_stations()), 1)

    def test_raises_for_missing_required_columns(self):
        self.csv_path.write_text("Truckstop Name,Retail Price\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "missing one or more required"):
            get_fuel_stations()

    def test_raises_for_invalid_station_price(self):
        self.csv_path.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "12,TEST STATION,I-10 EXIT 1,Phoenix,AZ,123,unknown\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "row 2"):
            get_fuel_stations()


class StationGeocodingCommandTests(TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.cache_path = Path(self.temp_directory.name) / "station-coordinates.csv"
        self.cache_patcher = patch.object(
            geocode_fuel_stations, "COORDINATE_CACHE_PATH", self.cache_path
        )
        self.cache_patcher.start()
        self.addCleanup(self.cache_patcher.stop)
        self.stations_patcher = patch(
            "routing.management.commands.geocode_fuel_stations.get_fuel_stations",
            return_value=(
                FuelStation("1", "Station A", "I-10", "Phoenix", "AZ", "1", 3.5),
                FuelStation("2", "Station B", " I-10 ", " phoenix ", "az", "1", 3.4),
                FuelStation("3", "Station C", "US-1", "Miami", "FL", "2", 3.6),
            ),
        )
        self.stations_patcher.start()
        self.addCleanup(self.stations_patcher.stop)

    @patch("routing.management.commands.geocode_fuel_stations.geocode")
    def test_dry_run_reports_work_without_calling_geocoder(self, mock_geocode):
        output = StringIO()

        call_command("geocode_fuel_stations", dry_run=True, stdout=output)

        self.assertIn("2 unique station locations", output.getvalue())
        self.assertIn("2 would be geocoded", output.getvalue())
        mock_geocode.assert_not_called()
        self.assertFalse(self.cache_path.exists())

    @patch(
        "routing.management.commands.geocode_fuel_stations.geocode",
        side_effect=[(33.4484, -112.074), (25.7617, -80.1918)],
    )
    def test_deduplicates_and_resumes_from_coordinate_cache(self, mock_geocode):
        call_command("geocode_fuel_stations", limit=1, delay=0, stdout=StringIO())

        mock_geocode.assert_called_once_with("I-10, Phoenix, AZ, USA")
        call_command("geocode_fuel_stations", limit=1, delay=0, stdout=StringIO())

        self.assertEqual(mock_geocode.call_count, 2)
        with self.cache_path.open(newline="", encoding="utf-8") as cache_file:
            rows = list(csv.DictReader(cache_file))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["latitude"], "33.4484")
        self.assertEqual(rows[1]["longitude"], "-80.1918")

    @patch(
        "routing.management.commands.geocode_fuel_stations.geocode",
        side_effect=requests.Timeout("timed out"),
    )
    def test_external_failure_stops_and_keeps_cache_file(self, _mock_geocode):
        self.cache_path.write_text(
            "location_key,latitude,longitude\n"
            "i-10|phoenix|az,33.4484,-112.074\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CommandError, "Geocoding stopped"):
            call_command("geocode_fuel_stations", delay=0)

        with self.cache_path.open(newline="", encoding="utf-8") as cache_file:
            rows = list(csv.DictReader(cache_file))
        self.assertEqual(rows[0]["latitude"], "33.4484")


class GeocodingServiceTests(TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("routing.services.geocoding.requests.get")
    def test_geocode_requires_api_key(self, mock_get):
        with self.assertRaisesRegex(RuntimeError, "ORS_API_KEY"):
            geocode("New York, NY")
        mock_get.assert_not_called()

    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.geocoding.requests.get")
    def test_geocode_returns_latitude_and_longitude(self, mock_get):
        mock_get.return_value.json.return_value = {
            "features": [
                {"geometry": {"coordinates": [-73.935242, 40.73061]}}
            ]
        }

        self.assertEqual(geocode("New York, NY"), (40.73061, -73.935242))
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 10)

    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.geocoding.requests.get")
    def test_geocode_raises_when_location_is_not_found(self, mock_get):
        mock_get.return_value.json.return_value = {"features": []}

        with self.assertRaisesRegex(ValueError, "Location not found"):
            geocode("Unknown")

    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.geocoding.requests.get")
    def test_geocode_rejects_unexpected_response(self, mock_get):
        mock_get.return_value.json.return_value = {"features": [{}]}

        with self.assertRaisesRegex(RuntimeError, "Unexpected response"):
            geocode("New York, NY")


class RoutingServiceTests(TestCase):
    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.routing.requests.post")
    def test_get_route_sends_coordinates_and_returns_geometry(self, mock_post):
        geometry = {
            "type": "LineString",
            "coordinates": [[-74.0, 40.7], [-87.6, 41.9]],
        }
        mock_post.return_value.json.return_value = {
            "features": [
                {
                    "geometry": geometry,
                    "properties": {"summary": {"distance": 1270000}},
                }
            ]
        }

        result = get_route((40.7, -74.0), (41.9, -87.6))

        self.assertEqual(
            result,
            {
                "distance_meters": 1270000,
                "distance_miles": 1270000 / 1609.344,
                "geometry": geometry,
            },
        )
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["coordinates"],
            [[-74.0, 40.7], [-87.6, 41.9]],
        )
        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Authorization"], "test-key"
        )
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 20)

    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.routing.requests.post")
    def test_get_route_rejects_unexpected_response(self, mock_post):
        mock_post.return_value.json.return_value = {"features": []}

        with self.assertRaisesRegex(RuntimeError, "Unexpected response"):
            get_route((40.7, -74.0), (41.9, -87.6))

    @patch.dict(os.environ, {"ORS_API_KEY": "test-key"})
    @patch("routing.services.routing.requests.post")
    def test_get_route_rejects_invalid_geometry(self, mock_post):
        mock_post.return_value.json.return_value = {
            "features": [
                {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-74.0, 40.7], ["invalid", 41.9]],
                    },
                    "properties": {"summary": {"distance": 1270000}},
                }
            ]
        }

        with self.assertRaisesRegex(RuntimeError, "Unexpected response"):
            get_route((40.7, -74.0), (41.9, -87.6))


class RouteViewGeocodingTests(TestCase):
    @patch("routing.views.get_route")
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    def test_post_geocodes_locations_and_returns_route(
        self, mock_geocode, mock_get_route
    ):
        mock_get_route.return_value = {
            "distance_meters": 1270000,
            "distance_miles": 1270000 / 1609.344,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-74.0, 40.7], [-87.6, 41.9]],
            },
        }
        response = self.client.post(
            "/api/route/",
            data=json.dumps({"start": "New York, NY", "finish": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["start"],
            {
                "address": "New York, NY",
                "latitude": 40.7,
                "longitude": -74.0,
            },
        )
        self.assertEqual(
            response.json()["finish"],
            {
                "address": "Chicago, IL",
                "latitude": 41.9,
                "longitude": -87.6,
            },
        )
        self.assertEqual(
            response.json()["route"]["distance_meters"], 1270000
        )
        self.assertAlmostEqual(
            response.json()["route"]["distance_miles"], 789.14, places=2
        )
        self.assertEqual(
            response.json()["route"]["geometry"]["coordinates"],
            [[-74.0, 40.7], [-87.6, 41.9]],
        )
        self.assertEqual(mock_get_route.call_count, 1)
        self.assertEqual(mock_geocode.call_count, 2)

    @patch("routing.views.geocode", side_effect=ValueError("Location not found"))
    def test_post_returns_bad_request_when_location_is_not_found(self, _mock_geocode):
        response = self.client.post(
            "/api/route/",
            data=json.dumps({"start": "Unknown", "finish": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Location not found")

    @patch(
        "routing.views.geocode",
        side_effect=requests.ConnectionError("Connection failed"),
    )
    def test_post_returns_bad_gateway_when_geocoding_is_unavailable(
        self, _mock_geocode
    ):
        response = self.client.post(
            "/api/route/",
            data=json.dumps({"start": "New York, NY", "finish": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"], "The geocoding service is unavailable"
        )

    @patch("routing.views.get_route", side_effect=requests.Timeout)
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    def test_post_returns_bad_gateway_when_routing_is_unavailable(
        self, _mock_geocode, _mock_get_route
    ):
        response = self.client.post(
            "/api/route/",
            data=json.dumps({"start": "New York, NY", "finish": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"], "The routing service is unavailable"
        )
