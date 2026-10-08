import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FUEL_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "fuel-prices.csv"


@dataclass(frozen=True)
class FuelStation:
    station_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price: float


@lru_cache(maxsize=1)
def get_fuel_stations():
    with FUEL_DATA_PATH.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = {
            "OPIS Truckstop ID",
            "Truckstop Name",
            "Address",
            "City",
            "State",
            "Rack ID",
            "Retail Price",
        }
        if not required_columns.issubset(reader.fieldnames or []):
            raise ValueError("Fuel CSV is missing one or more required columns.")

        stations = []
        for row_number, row in enumerate(reader, start=2):
            try:
                station = FuelStation(
                    station_id=row["OPIS Truckstop ID"].strip(),
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=row["City"].strip(),
                    state=row["State"].strip(),
                    rack_id=row["Rack ID"].strip(),
                    price=float(row["Retail Price"]),
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid fuel CSV data on row {row_number}."
                ) from exc
            stations.append(station)

    return tuple(stations)
