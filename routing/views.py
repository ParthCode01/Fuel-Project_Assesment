import json

import requests
from django.http import JsonResponse
from routing.services.geocoding import geocode
from routing.services.routing import get_route


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
    })