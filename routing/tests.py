import csv
import json
import math
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from routing.services import station_proximity
from routing.services import fuel_data
from routing.services.fuel_data import (
    FuelStation,
    get_fuel_stations,
    get_station_coordinates,
)
from routing.services.geocoding import geocode
from routing.services.routing import get_route
from routing.services.station_proximity import find_stations_near_route
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
        self.coordinate_path_patcher = patch.object(
            fuel_data, "STATION_COORDINATES_PATH", self.csv_path
        )
        self.coordinate_path_patcher.start()
        self.addCleanup(self.coordinate_path_patcher.stop)
        get_station_coordinates.cache_clear()
        self.addCleanup(get_station_coordinates.cache_clear)

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

    def test_loads_coordinate_cache(self):
        self.csv_path.write_text(
            "location_key,latitude,longitude\n"
            "i-10|phoenix|az,33.4484,-112.074\n",
            encoding="utf-8",
        )

        self.assertEqual(
            get_station_coordinates(),
            {"i-10|phoenix|az": (33.4484, -112.074)},
        )


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


class StationProximityTests(TestCase):
    def setUp(self):
        self.station = FuelStation(
            "1", "Test Station", "I-10", "Phoenix", "AZ", "1", 3.5
        )
        self.stations_patcher = patch.object(
            station_proximity, "get_fuel_stations", return_value=(self.station,)
        )
        self.coordinates_patcher = patch.object(
            station_proximity,
            "get_station_coordinates",
            return_value={"i-10|phoenix|az": (33.5, -112.0)},
        )
        self.stations_patcher.start()
        self.coordinates_patcher.start()
        self.addCleanup(self.stations_patcher.stop)
        self.addCleanup(self.coordinates_patcher.stop)

    def test_finds_station_close_to_route(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
        }

        nearby = find_stations_near_route(route, max_distance_miles=25)

        self.assertEqual(len(nearby), 1)
        self.assertIs(nearby[0].station, self.station)
        self.assertAlmostEqual(nearby[0].distance_to_route_miles, 0, places=3)
        self.assertAlmostEqual(
            nearby[0].distance_along_route_miles,
            69.0 * math.cos(math.radians(33.5)),
            delta=0.5,
        )

    def test_orders_stations_by_distance_along_route(self):
        first_station = FuelStation(
            "first", "First", "US-1", "City A", "AZ", "1", 3.6
        )
        second_station = FuelStation(
            "second", "Second", "US-2", "City B", "AZ", "2", 3.5
        )
        self.stations_patcher.stop()
        self.stations_patcher = patch.object(
            station_proximity,
            "get_fuel_stations",
            return_value=(second_station, first_station),
        )
        self.stations_patcher.start()
        self.addCleanup(self.stations_patcher.stop)
        self.coordinates_patcher.stop()
        self.coordinates_patcher = patch.object(
            station_proximity,
            "get_station_coordinates",
            return_value={
                "us-1|city a|az": (33.5, -112.5),
                "us-2|city b|az": (33.5, -111.8),
            },
        )
        self.coordinates_patcher.start()
        self.addCleanup(self.coordinates_patcher.stop)
        route = {
            "type": "LineString",
            "coordinates": [
                [-113.0, 33.5],
                [-112.0, 33.5],
                [-111.0, 33.5],
            ],
        }

        nearby = find_stations_near_route(route, max_distance_miles=25)

        self.assertEqual(
            [item.station.station_id for item in nearby], ["first", "second"]
        )
        self.assertLess(
            nearby[0].distance_along_route_miles,
            nearby[1].distance_along_route_miles,
        )

    def test_excludes_station_outside_proximity_limit(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
        }
        self.coordinates_patcher.stop()
        self.coordinates_patcher = patch.object(
            station_proximity,
            "get_station_coordinates",
            return_value={"i-10|phoenix|az": (35.0, -112.0)},
        )
        self.coordinates_patcher.start()
        self.addCleanup(self.coordinates_patcher.stop)

        self.assertEqual(find_stations_near_route(route, max_distance_miles=25), ())

    def test_rejects_invalid_route_geometry(self):
        with self.assertRaisesRegex(ValueError, "LineString"):
            find_stations_near_route({"type": "Point", "coordinates": [-112, 33]})

    def test_requires_positive_proximity_limit(self):
        with self.assertRaisesRegex(ValueError, "positive number"):
            find_stations_near_route(
                {
                    "type": "LineString",
                    "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
                },
                max_distance_miles=0,
            )

    def test_identifies_reachable_stations_by_route_position(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
        }
        station = FuelStation("route-stop", "Route Stop", "I-10", "Phoenix", "AZ", "1", 3.5)
        nearby = (
            station_proximity.NearbyFuelStation(
                station,
                33.5,
                -112.0,
                0,
                50.0,
            ),
        )

        reachable = station_proximity.get_reachable_stations(route, nearby)

        self.assertEqual(len(reachable), 1)
        self.assertEqual(reachable[0]["station"], station)
        self.assertGreater(reachable[0]["distance_remaining_after_station_miles"], 0)

    def test_selects_safe_cheapest_stops_within_range(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-112.0, 33.5], [-111.0, 33.5]],
        }
        cheaper = FuelStation("cheap", "Cheap Stop", "I-10", "Phoenix", "AZ", "1", 3.0)
        pricier = FuelStation("pricy", "Pricey Stop", "I-10", "Mesa", "AZ", "2", 4.5)
        far = FuelStation("far", "Far Stop", "I-10", "Tucson", "AZ", "3", 2.8)
        nearby = (
            station_proximity.NearbyFuelStation(cheaper, 33.5, -112.5, 0, 50.0),
            station_proximity.NearbyFuelStation(pricier, 33.5, -111.5, 0, 120.0),
            station_proximity.NearbyFuelStation(far, 33.5, -112.2, 0, 200.0),
        )

        selected = station_proximity.select_fuel_stops(route, nearby)

        self.assertEqual(selected[0]["station"], cheaper)
        self.assertGreater(selected[0]["distance_from_current_miles"], 0)
        self.assertGreater(selected[0]["fuel_cost"], 0)

    def test_rejects_route_with_no_safe_fuel_stop(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
        }
        distant_station = FuelStation("distant", "Distant", "I-10", "Yuma", "AZ", "1", 3.1)
        nearby = (
            station_proximity.NearbyFuelStation(distant_station, 33.5, -112.0, 0, 700.0),
        )

        with self.assertRaisesRegex(ValueError, "No reachable fuel station"):
            station_proximity.select_fuel_stops(route, nearby)

    def test_calculates_fuel_needed_for_distance(self):
        self.assertAlmostEqual(
            station_proximity.calculate_fuel_needed(100, 10),
            10.0,
        )
        self.assertAlmostEqual(
            station_proximity.calculate_fuel_needed(0, 10),
            0.0,
        )
        with self.assertRaisesRegex(ValueError, "distance_miles"):
            station_proximity.calculate_fuel_needed(-5)

    def test_calculates_total_fuel_cost(self):
        selected_stops = (
            {"gallons_needed": 10, "price_per_gallon": 3.5},
            {"gallons_needed": 5, "price_per_gallon": 4.0},
        )

        self.assertAlmostEqual(
            station_proximity.calculate_total_fuel_cost(selected_stops),
            55.0,
        )

        with self.assertRaisesRegex(ValueError, "gallons_needed"):
            station_proximity.calculate_total_fuel_cost(
                ({"gallons_needed": -1, "price_per_gallon": 3.0},)
            )

    def test_caches_route_processing_values(self):
        route = {
            "type": "LineString",
            "coordinates": [[-113.0, 33.5], [-111.0, 33.5]],
        }
        route_points = station_proximity._validate_route_geometry(route)

        self.assertIs(
            station_proximity._sample_route(route_points),
            station_proximity._sample_route(route_points),
        )
        self.assertIs(
            station_proximity._distance_from_start_miles(route_points),
            station_proximity._distance_from_start_miles(route_points),
        )


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
    @patch("routing.views.select_fuel_stops")
    @patch("routing.views.find_stations_near_route")
    @patch("routing.views.get_route")
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    def test_post_geocodes_locations_and_returns_route(
        self,
        mock_geocode,
        mock_get_route,
        mock_find_stations_near_route,
        mock_select_fuel_stops,
    ):
        mock_get_route.return_value = {
            "distance_meters": 1270000,
            "distance_miles": 1270000 / 1609.344,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-74.0, 40.7], [-87.6, 41.9]],
            },
        }
        mock_find_stations_near_route.return_value = (
            station_proximity.NearbyFuelStation(
                FuelStation("1", "Test Station", "Main St", "Chicago", "IL", "1", 3.25),
                41.9,
                -87.6,
                0.0,
                10.0,
            ),
        )
        mock_select_fuel_stops.return_value = (
            {
                "station": FuelStation("1", "Test Station", "Main St", "Chicago", "IL", "1", 3.25),
                "distance_along_route_miles": 10.0,
                "distance_from_current_miles": 10.0,
                "gallons_needed": 1.0,
                "fuel_cost": 3.25,
            },
        )
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
        self.assertEqual(response.json()["total_gallons"], 1.0)
        self.assertEqual(response.json()["total_fuel_cost"], 3.25)
        self.assertEqual(response.json()["selected_stops"][0]["name"], "Test Station")
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

    @patch("routing.views.get_route")
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    @patch("routing.views.find_stations_near_route", return_value=())
    def test_post_rejects_non_object_json_body(
        self, _mock_find_stations_near_route, _mock_geocode, _mock_get_route
    ):
        response = self.client.post(
            "/api/route/",
            data=json.dumps(["not", "an", "object"]),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.json()["error"])

    @patch("routing.views.select_fuel_stops", side_effect=ValueError("No reachable fuel station"))
    @patch("routing.views.find_stations_near_route")
    @patch("routing.views.get_route")
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    def test_post_returns_conflict_when_route_has_no_reachable_fuel_stations(
        self, _mock_geocode, _mock_get_route, _mock_find_stations_near_route, _mock_select_fuel_stops
    ):
        _mock_find_stations_near_route.return_value = (
            station_proximity.NearbyFuelStation(
                FuelStation("1", "Test Station", "Main St", "Chicago", "IL", "1", 3.25),
                41.9,
                -87.6,
                0.0,
                10.0,
            ),
        )

        response = self.client.post(
            "/api/route/",
            data=json.dumps({"start": "New York, NY", "finish": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "No reachable fuel station")

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
