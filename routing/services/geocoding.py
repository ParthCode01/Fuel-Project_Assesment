import math
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "config" / ".env")

GEOCODING_URL = "https://api.openrouteservice.org/geocode/search"


def geocode(address):
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        raise RuntimeError("ORS_API_KEY is not configured.")

    params = {
        "api_key": api_key,
        "text": address,
        "boundary.country": "US",
    }

    response = requests.get(GEOCODING_URL, params=params, timeout=10)
    response.raise_for_status()
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("Unexpected response from geocoding service.") from exc

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise RuntimeError("Unexpected response from geocoding service.")
    if not features:
        raise ValueError(f"Location not found: {address}")

    try:
        coordinates = features[0]["geometry"]["coordinates"]
        longitude, latitude = coordinates
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("Unexpected response from geocoding service.") from exc

    if (
        not isinstance(latitude, (int, float))
        or isinstance(latitude, bool)
        or not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not isinstance(longitude, (int, float))
        or isinstance(longitude, bool)
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise RuntimeError("Unexpected response from geocoding service.")

    return latitude, longitude