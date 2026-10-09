import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings
from routing.services.fuel_data import (
    FuelStation,
    get_fuel_stations,
    get_station_coordinates,
    station_location_key,
)

GRID_CELL_DEGREES = 0.25
MILES_PER_LATITUDE_DEGREE = 69.0
SAMPLE_SPACING_MILES = 5.0
MAX_VEHICLE_RANGE_MILES = settings.VEHICLE_MAX_RANGE_MILES
VEHICLE_MPG = settings.VEHICLE_MILES_PER_GALLON
MAX_STATION_DISTANCE_MILES = settings.FUEL_STATION_ROUTE_DISTANCE_MILES


@dataclass(frozen=True)
class NearbyFuelStation:
    station: FuelStation
    latitude: float
    longitude: float
    distance_to_route_miles: float
    distance_along_route_miles: float


def _validate_route_geometry(route_geometry):
    if not isinstance(route_geometry, dict) or route_geometry.get("type") != "LineString":
        raise ValueError("Route geometry must be a GeoJSON LineString.")

    coordinates = route_geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise ValueError("Route geometry must contain at least two coordinates.")

    points = []
    for coordinate in coordinates:
        if (
            not isinstance(coordinate, (list, tuple))
            or len(coordinate) < 2
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in coordinate[:2]
            )
        ):
            raise ValueError("Route geometry contains an invalid coordinate.")
        longitude, latitude = coordinate[:2]
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("Route geometry contains an out-of-range coordinate.")
        points.append((longitude, latitude))

    return tuple(points)


def _haversine_miles(first, second):
    longitude_1, latitude_1 = first
    longitude_2, latitude_2 = second
    latitude_delta = math.radians(latitude_2 - latitude_1)
    longitude_delta = math.radians(longitude_2 - longitude_1)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(latitude_1))
        * math.cos(math.radians(latitude_2))
        * math.sin(longitude_delta / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return 3958.7613 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


@lru_cache(maxsize=128)
def _sample_route(points):
    points = tuple(points)
    samples = [points[0]]
    for start, end in zip(points, points[1:]):
        segment_length = _haversine_miles(start, end)
        steps = max(1, math.ceil(segment_length / SAMPLE_SPACING_MILES))
        for step in range(1, steps + 1):
            fraction = step / steps
            samples.append(
                (
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            )
    return tuple(samples)


def _project_onto_segment_miles(point, start, end):
    longitude, latitude = point
    scale_x = MILES_PER_LATITUDE_DEGREE * math.cos(math.radians(latitude))
    point_x, point_y = longitude * scale_x, latitude * MILES_PER_LATITUDE_DEGREE
    start_x, start_y = start[0] * scale_x, start[1] * MILES_PER_LATITUDE_DEGREE
    end_x, end_y = end[0] * scale_x, end[1] * MILES_PER_LATITUDE_DEGREE
    segment_x, segment_y = end_x - start_x, end_y - start_y
    segment_length_squared = segment_x**2 + segment_y**2
    if segment_length_squared == 0:
        fraction = 0.0
    else:
        fraction = max(
            0.0,
            min(
                1.0,
                ((point_x - start_x) * segment_x + (point_y - start_y) * segment_y)
                / segment_length_squared,
            ),
        )
    nearest_x = start_x + fraction * segment_x
    nearest_y = start_y + fraction * segment_y
    return math.hypot(point_x - nearest_x, point_y - nearest_y), fraction


def _project_onto_route_miles(point, route_points):
    distance_from_start = 0.0
    closest_distance = math.inf
    closest_position = 0.0

    for start, end in zip(route_points, route_points[1:]):
        segment_length = _haversine_miles(start, end)
        distance_to_segment, fraction = _project_onto_segment_miles(
            point, start, end
        )
        if distance_to_segment < closest_distance:
            closest_distance = distance_to_segment
            closest_position = distance_from_start + segment_length * fraction
        distance_from_start += segment_length

    return closest_distance, closest_position


def _cell(latitude, longitude):
    return (
        math.floor(latitude / GRID_CELL_DEGREES),
        math.floor(longitude / GRID_CELL_DEGREES),
    )


@lru_cache(maxsize=128)
def _distance_from_start_miles(route_points):
    route_points = tuple(route_points)
    cumulative = [0.0]
    total = 0.0
    for start, end in zip(route_points, route_points[1:]):
        total += _haversine_miles(start, end)
        cumulative.append(total)
    return tuple(cumulative)


def find_stations_near_route(route_geometry, max_distance_miles=None):
    if max_distance_miles is None:
        max_distance_miles = MAX_STATION_DISTANCE_MILES
    if (
        not isinstance(max_distance_miles, (int, float))
        or not math.isfinite(max_distance_miles)
        or max_distance_miles <= 0
    ):
        raise ValueError("max_distance_miles must be a positive number.")

    route_points = _validate_route_geometry(route_geometry)
    coordinates_by_key = get_station_coordinates()
    stations_by_cell = defaultdict(list)
    for station in get_fuel_stations():
        coordinates = coordinates_by_key.get(station_location_key(station))
        if coordinates is not None:
            latitude, longitude = coordinates
            stations_by_cell[_cell(latitude, longitude)].append(
                (station, latitude, longitude)
            )

    cell_candidates = {}
    search_radius = max_distance_miles + SAMPLE_SPACING_MILES / 2
    for longitude, latitude in _sample_route(route_points):
        latitude_radius = search_radius / MILES_PER_LATITUDE_DEGREE
        cosine = max(abs(math.cos(math.radians(latitude))), 0.01)
        longitude_radius = search_radius / (MILES_PER_LATITUDE_DEGREE * cosine)
        min_latitude_cell = math.floor(
            (latitude - latitude_radius) / GRID_CELL_DEGREES
        )
        max_latitude_cell = math.floor(
            (latitude + latitude_radius) / GRID_CELL_DEGREES
        )
        min_longitude_cell = math.floor(
            (longitude - longitude_radius) / GRID_CELL_DEGREES
        )
        max_longitude_cell = math.floor(
            (longitude + longitude_radius) / GRID_CELL_DEGREES
        )
        for latitude_cell in range(min_latitude_cell, max_latitude_cell + 1):
            for longitude_cell in range(min_longitude_cell, max_longitude_cell + 1):
                for station_data in stations_by_cell.get(
                    (latitude_cell, longitude_cell), ()
                ):
                    cell_candidates[station_data[0]] = station_data

    nearby_stations = []
    for station, latitude, longitude in cell_candidates.values():
        distance, distance_along_route = _project_onto_route_miles(
            (longitude, latitude), route_points
        )
        if distance <= max_distance_miles:
            nearby_stations.append(
                NearbyFuelStation(
                    station,
                    latitude,
                    longitude,
                    distance,
                    distance_along_route,
                )
            )

    return tuple(
        sorted(
            nearby_stations,
            key=lambda nearby: nearby.distance_along_route_miles,
        )
    )


def _route_length_miles(route_geometry):
    route_points = _validate_route_geometry(route_geometry)
    return _distance_from_start_miles(route_points)[-1]


def calculate_fuel_needed(distance_miles, vehicle_mpg=VEHICLE_MPG):
    if (
        not isinstance(distance_miles, (int, float))
        or not math.isfinite(distance_miles)
        or distance_miles < 0
    ):
        raise ValueError("distance_miles must be a non-negative number.")
    if (
        not isinstance(vehicle_mpg, (int, float))
        or not math.isfinite(vehicle_mpg)
        or vehicle_mpg <= 0
    ):
        raise ValueError("vehicle_mpg must be a positive number.")
    return distance_miles / vehicle_mpg


def calculate_total_fuel_cost(selected_stops):
    if not isinstance(selected_stops, (list, tuple)):
        raise ValueError("selected_stops must be a list or tuple of stop records.")

    total_cost = 0.0
    for stop in selected_stops:
        if not isinstance(stop, dict):
            raise ValueError("Each fuel stop must be a dictionary with gallons and price.")

        gallons = stop.get("gallons_needed")
        price = stop.get("price_per_gallon")
        if (
            not isinstance(gallons, (int, float))
            or not math.isfinite(gallons)
            or gallons < 0
        ):
            raise ValueError("Each fuel stop must include a valid gallons_needed value.")
        if (
            not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price < 0
        ):
            raise ValueError("Each fuel stop must include a valid price_per_gallon value.")

        total_cost += gallons * price

    return total_cost


def select_fuel_stops(
    route_geometry,
    stations=None,
    max_range_miles=MAX_VEHICLE_RANGE_MILES,
    vehicle_mpg=VEHICLE_MPG,
):
    if (
        not isinstance(max_range_miles, (int, float))
        or not math.isfinite(max_range_miles)
        or max_range_miles <= 0
    ):
        raise ValueError("max_range_miles must be a positive number.")
    if (
        not isinstance(vehicle_mpg, (int, float))
        or not math.isfinite(vehicle_mpg)
        or vehicle_mpg <= 0
    ):
        raise ValueError("vehicle_mpg must be a positive number.")

    if stations is None:
        stations = find_stations_near_route(route_geometry)
    total_route_distance = _route_length_miles(route_geometry)
    if total_route_distance <= max_range_miles:
        return ()

    stations_by_location = {}
    for nearby in stations:
        position = nearby.distance_along_route_miles
        price = nearby.station.price
        if (
            not math.isfinite(position)
            or position <= 0
            or position >= total_route_distance
            or not math.isfinite(price)
            or price <= 0
        ):
            continue

        location_key = (
            nearby.station.address.casefold(),
            nearby.station.city.casefold(),
            nearby.station.state.casefold(),
        )
        current = stations_by_location.get(location_key)
        if current is None or price < current.station.price:
            stations_by_location[location_key] = nearby

    ordered_stations = tuple(
        sorted(
            stations_by_location.values(),
            key=lambda nearby: (
                nearby.distance_along_route_miles,
                nearby.station.price,
                nearby.station.station_id,
            ),
        )
    )
    if not ordered_stations:
        raise ValueError("No nearby fuel stations are available along the route.")

    count = len(ordered_stations)
    cost_to_finish = [math.inf] * count
    next_station_index = [None] * count

    for index in range(count - 1, -1, -1):
        station = ordered_stations[index]
        final_leg = total_route_distance - station.distance_along_route_miles
        if final_leg <= max_range_miles:
            cost_to_finish[index] = (
                calculate_fuel_needed(final_leg, vehicle_mpg) * station.station.price
            )

        for next_index in range(index + 1, count):
            next_station = ordered_stations[next_index]
            leg_distance = (
                next_station.distance_along_route_miles
                - station.distance_along_route_miles
            )
            if leg_distance <= 0:
                continue
            if leg_distance > max_range_miles:
                break

            candidate_cost = (
                calculate_fuel_needed(leg_distance, vehicle_mpg)
                * next_station.station.price
                + cost_to_finish[next_index]
            )
            if candidate_cost < cost_to_finish[index]:
                cost_to_finish[index] = candidate_cost
                next_station_index[index] = next_index

    first_index = None
    best_total_cost = math.inf
    for index, station in enumerate(ordered_stations):
        first_leg = station.distance_along_route_miles
        if first_leg > max_range_miles:
            break

        candidate_cost = (
            calculate_fuel_needed(first_leg, vehicle_mpg)
            * station.station.price
            + cost_to_finish[index]
        )
        if candidate_cost < best_total_cost:
            first_index = index
            best_total_cost = candidate_cost

    if first_index is None or not math.isfinite(best_total_cost):
        raise ValueError(
            "No safe fuel station satisfies the vehicle range and route constraints."
        )

    selected = []
    current_position = 0.0
    current_index = first_index
    while current_index is not None:
        station = ordered_stations[current_index]
        leg_distance = station.distance_along_route_miles - current_position
        leg_gallons = calculate_fuel_needed(leg_distance, vehicle_mpg)
        selected.append(
            {
                "station": station.station,
                "distance_along_route_miles": station.distance_along_route_miles,
                "distance_from_current_miles": leg_distance,
                "gallons_needed": leg_gallons,
                "fuel_cost": leg_gallons * station.station.price,
                "price_per_gallon": station.station.price,
            }
        )
        current_position = station.distance_along_route_miles
        current_index = next_station_index[current_index]

    return tuple(selected)
