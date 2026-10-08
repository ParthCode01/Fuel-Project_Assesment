import json

import requests
from django.http import JsonResponse
from routing.services.geocoding import geocode
from routing.services.routing import get_route
from routing.services.station_proximity import (
    calculate_total_fuel_cost,
    find_stations_near_route,
    select_fuel_stops,
)


def _serialize_selected_stop(stop):
    station = stop["station"]
    return {
        "station_id": station.station_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "rack_id": station.rack_id,
        "price_per_gallon": station.price,
        "distance_along_route_miles": stop["distance_along_route_miles"],
        "distance_from_current_miles": stop["distance_from_current_miles"],
        "gallons_needed": stop["gallons_needed"],
        "fuel_cost": stop["fuel_cost"],
    }


def route_view(request):
    if request.method != "POST":
        return JsonResponse(
            {"error": "Only POST requests are allowed"},
            status=405
        )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400
        )

    start = data.get("start")
    finish = data.get("finish")

    if not start or not finish:
        return JsonResponse(
            {"error": "start and finish are required"},
            status=400
        )

    try:
        start_coordinates = geocode(start)
        finish_coordinates = geocode(finish)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except requests.RequestException:
        return JsonResponse(
            {"error": "The geocoding service is unavailable"},
            status=502
        )

    try:
        route = get_route(start_coordinates, finish_coordinates)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except requests.RequestException:
        return JsonResponse(
            {"error": "The routing service is unavailable"},
            status=502
        )

    nearby_stations = ()
    selected_stops = ()
    total_gallons = 0.0
    total_fuel_cost = 0.0

    try:
        nearby_stations = find_stations_near_route(route["geometry"], max_distance_miles=25)
    except (ValueError, FileNotFoundError):
        nearby_stations = ()

    if nearby_stations:
        try:
            selected_stops = select_fuel_stops(route["geometry"], nearby_stations)
        except ValueError:
            selected_stops = ()

    if selected_stops:
        total_gallons = sum(stop["gallons_needed"] for stop in selected_stops)
        total_fuel_cost = calculate_total_fuel_cost(
            tuple(
                {
                    "gallons_needed": stop["gallons_needed"],
                    "price_per_gallon": stop["station"].price,
                }
                for stop in selected_stops
            )
        )

    return JsonResponse({
        "message": "Route calculated",
        "start": {
            "address": start,
            "latitude": start_coordinates[0],
            "longitude": start_coordinates[1],
        },
        "finish": {
            "address": finish,
            "latitude": finish_coordinates[0],
            "longitude": finish_coordinates[1],
        },
        "route": route,
        "total_gallons": total_gallons,
        "total_fuel_cost": total_fuel_cost,
        "selected_stops": [_serialize_selected_stop(stop) for stop in selected_stops],
    })