import json

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from routing.services.geocoding import geocode
from routing.services.routing import get_route
from routing.services.station_proximity import (
    MAX_STATION_DISTANCE_MILES,
    MAX_VEHICLE_RANGE_MILES,
    VEHICLE_MPG,
    calculate_fuel_needed,
    calculate_total_fuel_cost,
    find_stations_near_route,
    select_fuel_stops,
)
from routing.services.fuel_data import get_station_coordinate_coverage


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


def _external_service_error(service_name, exc):
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    status = 503 if status_code == 429 else 502
    message = (
        f"The {service_name} service rate limit was reached"
        if status_code == 429
        else f"The {service_name} service is unavailable"
    )
    return JsonResponse({"error": message}, status=status)


@csrf_exempt
def route_view(request):
    if request.method != "POST":
        return JsonResponse(
            {"error": "Only POST requests are allowed"},
            status=405,
        )

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict):
        return JsonResponse(
            {"error": "Request body must be a JSON object with start and finish."},
            status=400,
        )

    start = data.get("start")
    finish = data.get("finish")

    if not isinstance(start, str) or not start.strip():
        return JsonResponse({"error": "start is required"}, status=400)
    if not isinstance(finish, str) or not finish.strip():
        return JsonResponse({"error": "finish is required"}, status=400)

    start = start.strip()
    finish = finish.strip()

    try:
        start_coordinates = geocode(start)
        finish_coordinates = geocode(finish)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except requests.RequestException as exc:
        return _external_service_error("geocoding", exc)

    try:
        route = get_route(start_coordinates, finish_coordinates)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except requests.RequestException as exc:
        return _external_service_error("routing", exc)

    try:
        nearby_stations = find_stations_near_route(
            route["geometry"],
            max_distance_miles=MAX_STATION_DISTANCE_MILES,
        )
    except FileNotFoundError:
        return JsonResponse(
            {
                "error": (
                    "Fuel station coordinates are not available yet. "
                    "Run `python manage.py geocode_fuel_stations` to build the "
                    "coordinate cache."
                )
            },
            status=503,
        )
    except ValueError as exc:
        return JsonResponse(
            {"error": f"Fuel station data is invalid: {exc}"},
            status=503,
        )
    except OSError:
        return JsonResponse(
            {"error": "Fuel station files could not be read."},
            status=503,
        )

    try:
        station_coverage = get_station_coordinate_coverage()
    except (ValueError, FileNotFoundError) as exc:
        return JsonResponse(
            {"error": f"Fuel station data is unavailable: {exc}"},
            status=503,
        )
    except OSError:
        return JsonResponse(
            {"error": "Fuel station files could not be read."},
            status=503,
        )

    if not nearby_stations:
        if not station_coverage["complete"]:
            return JsonResponse(
                {
                    "error": (
                        "The station coordinate cache is incomplete, so the API "
                        "cannot determine whether stations are near this route."
                    ),
                    "station_cache": station_coverage,
                },
                status=503,
            )
        return JsonResponse(
            {"error": "No cached fuel stations were found near this route."},
            status=409,
        )

    try:
        selected_stops = select_fuel_stops(route["geometry"], nearby_stations)
    except ValueError as exc:
        if not station_coverage["complete"]:
            return JsonResponse(
                {
                    "error": (
                        "The station coordinate cache is incomplete, so the API "
                        "cannot establish whether a safe fuel-stop plan exists."
                    ),
                    "station_cache": station_coverage,
                },
                status=503,
            )
        return JsonResponse({"error": str(exc)}, status=409)

    total_gallons = calculate_fuel_needed(route["distance_miles"], VEHICLE_MPG)
    cost_reference = None
    if selected_stops:
        gallons_before_final_leg = sum(
            stop["gallons_needed"] for stop in selected_stops
        )
        final_leg_gallons = max(0.0, total_gallons - gallons_before_final_leg)
        selected_stops[-1]["gallons_needed"] += final_leg_gallons
        selected_stops[-1]["fuel_cost"] += (
            final_leg_gallons * selected_stops[-1]["station"].price
        )
        total_fuel_cost = calculate_total_fuel_cost(
            tuple(
                {
                    "gallons_needed": stop["gallons_needed"],
                    "price_per_gallon": stop["station"].price,
                }
                for stop in selected_stops
            )
        )
    else:
        reference_station = min(
            nearby_stations,
            key=lambda nearby: nearby.station.price,
        ).station
        total_fuel_cost = total_gallons * reference_station.price
        cost_reference = {
            "station_id": reference_station.station_id,
            "name": reference_station.name,
            "price_per_gallon": reference_station.price,
        }

    total_fuel_cost = round(total_fuel_cost, 2)

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
        "fuel_cost_method": (
            "Estimated route fuel consumption is distance divided by MPG. A "
            "leg ending at a selected station is valued at that station's price; "
            "the final leg uses the last stop's price. The initial leg is valued "
            "at the first selected stop's price because the starting tank's "
            "purchase price is unknown. For trips requiring no stops, the "
            "cheapest nearby station is used as a price reference. This is an "
            "estimated consumption cost, not a record of fuel purchases."
        ),
        "fuel_cost_reference": cost_reference,
        "vehicle": {
            "max_range_miles": MAX_VEHICLE_RANGE_MILES,
            "miles_per_gallon": VEHICLE_MPG,
            "starting_fuel_assumption": (
                "Vehicle starts with a full tank capable of the configured maximum range."
            ),
        },
        "station_cache": station_coverage,
        "optimization_scope": (
            "Cached stations only; uncached locations may contain cheaper alternatives."
            if not station_coverage["complete"]
            else "All unique dataset station locations are geocoded."
        ),
    })