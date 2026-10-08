import json
import os
from unittest.mock import patch

import requests
from django.test import TestCase

from routing.services.geocoding import geocode


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


class RouteViewGeocodingTests(TestCase):
    @patch("routing.views.geocode", side_effect=[(40.7, -74.0), (41.9, -87.6)])
    def test_post_geocodes_start_and_finish(self, mock_geocode):
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
