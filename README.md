# Fuel Route Optimization API

A Django API for planning a route between two U.S. locations, finding nearby fuel stations, applying a 500-mile range constraint, and returning the most cost-effective stop plan based on fuel pricing.

## Features

- Validates start and finish locations
- Geocodes locations using OpenRouteService
- Calculates route distance and geometry using ORS driving directions
- Loads fuel station data from the provided CSV
- Geocodes and caches station coordinates to reduce repeated external calls
- Finds stations close to the route
- Orders stations by route position
- Chooses fuel stops that satisfy the vehicle safety constraint
- Calculates gallons used and total fuel spend
- Returns a clean JSON payload for use in Postman, curl, or browser-based API testing

## Tech stack

- Django
- Django REST Framework
- pandas
- requests
- python-dotenv

## Setup

1. Create a virtual environment and install dependencies:

   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt

2. Create your environment file:

   Copy `config/.env.example` to `config/.env`

3. Add your OpenRouteService API key:

   ORS_API_KEY=your_key_here

4. Run migrations:

   python manage.py migrate

5. Start the server:

   python manage.py runserver

## API endpoint

POST /api/route/

Request body:

```json
{
  "start": "New York, NY",
  "finish": "Chicago, IL"
}
```

Example response:

```json
{
  "message": "Route calculated",
  "start": {
    "address": "New York, NY",
    "latitude": 40.73061,
    "longitude": -73.935242
  },
  "finish": {
    "address": "Chicago, IL",
    "latitude": 41.8781,
    "longitude": -87.6298
  },
  "route": {
    "distance_meters": 1270000,
    "distance_miles": 789.14,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-73.935242, 40.73061], [-87.6298, 41.8781]]
    }
  },
  "total_gallons": 15.5,
  "total_fuel_cost": 49.75,
  "selected_stops": [
    {
      "station_id": "12345",
      "name": "Example Truckstop",
      "address": "I-90 Exit 42",
      "city": "Indiana",
      "state": "IN",
      "price_per_gallon": 3.25,
      "distance_along_route_miles": 150.0,
      "distance_from_current_miles": 150.0,
      "gallons_needed": 15.0,
      "fuel_cost": 48.75
    }
  ]
}
```

## Architecture

The project keeps external API logic inside service modules so the view stays thin.

- `routing/views.py` handles request validation and response shaping.
- `routing/services/geocoding.py` handles ORS geocoding.
- `routing/services/routing.py` handles ORS routing and route geometry parsing.
- `routing/services/fuel_data.py` loads and normalizes the fuel-price CSV.
- `routing/services/station_proximity.py` finds route-adjacent stations, orders them, checks range constraints, and selects fuel stops.

## Algorithm summary

The route-planning flow is:

1. Validate the request.
2. Geocode start and finish.
3. Call the ORS routing API once.
4. Load the fuel station list from the CSV.
5. Find stations near the computed route.
6. Sort stations by distance along the route.
7. Evaluate reachable stations using the 500-mile maximum range.
8. Select the safest, cost-aware stop plan while ensuring the vehicle never becomes stranded.
9. Calculate gallons needed from distance traveled.
10. Calculate total fuel cost and return JSON.

## Assumptions

- Vehicle maximum range is 500 miles.
- Vehicle economy is 10 MPG.
- Fuel required is distance / 10.
- The app assumes all routing and geocoding requests are for locations in the USA.
- Fuel-stop selection balances price and route safety, rather than simply choosing the globally cheapest station.
- The ORS API key is stored in `config/.env` and is never committed to source control.

## Testing

Run the project tests:

```bash
python manage.py test routing.tests
```

The test suite includes:

- request validation
- geocoding and routing behavior
- fuel-data loading
- station proximity checks
- reachability and stop selection
- fuel cost calculation
- API response validation

## Notes for GitHub submission

- The project is structured for readability and a clean backend assessment submission.
- External API integrations are isolated in service files.
- The CSV is cached in memory to avoid repeated reads from disk.
- Station geocoding is handled through a cache-preprocessing strategy to reduce unnecessary ORS calls.
- No secrets are checked into the repository.
