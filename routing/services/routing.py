import math
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "config" / ".env")

DIRECTIONS_URL = (
    "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
)
METERS_PER_MILE = 1609.344


def get_route(start_coordinates, finish_coordinates):
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        raise RuntimeError("ORS_API_KEY is not configured.")

    start_latitude, start_longitude = start_coordinates
    finish_latitude, finish_longitude = finish_coordinates
    payload = {
        "coordinates": [
            [start_longitude, start_latitude],
            [finish_longitude, finish_latitude],
        ]
    }

    response = requests.post(
        DIRECTIONS_URL,
        json=payload,
        headers={"Authorization": api_key},
        timeout=20,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("Unexpected response from routing service.") from exc

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list) or not features:
        raise RuntimeError("Unexpected response from routing service.")

    try:
        feature = features[0]
        geometry = feature["geometry"]
        distance_meters = feature["properties"]["summary"]["distance"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Unexpected response from routing service.") from exc

    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if (
        not isinstance(geometry, dict)
        or geometry.get("type") != "LineString"
        or not isinstance(coordinates, list)
        or len(coordinates) < 2
        or any(
            not isinstance(point, list)
            or len(point) < 2
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in point[:2]
            )
            or not -180 <= point[0] <= 180
            or not -90 <= point[1] <= 90
            for point in coordinates
        )
        or not isinstance(distance_meters, (int, float))
        or not math.isfinite(distance_meters)
        or distance_meters < 0
    ):
        raise RuntimeError("Unexpected response from routing service.")

    return {
        "distance_meters": distance_meters,
        "distance_miles": distance_meters / METERS_PER_MILE,
        "geometry": geometry,
    }
