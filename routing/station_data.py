import csv
import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import TextIO
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

REQUIRED_COLUMNS = (
    'OPIS Truckstop ID',
    'Truckstop Name',
    'Address',
    'City',
    'State',
    'Retail Price',
)


class StationDatasetError(Exception):
    """The configured station CSV cannot be used safely."""


@dataclass(frozen=True)
class FuelStation:
    station_id: str
    name: str
    address: str
    city: str
    state: str
    retail_price: Decimal
    latitude: float | None = None
    longitude: float | None = None

    @property
    def coordinate_input_hash(self) -> str:
        material = '\0'.join((
            self.station_id,
            self.address.casefold(),
            self.city.casefold(),
            self.state,
            settings.CENSUS_GEOCODER_BATCH_URL,
            settings.CENSUS_GEOCODER_BENCHMARK,
        ))
        return hashlib.sha256(material.encode('utf-8')).hexdigest()

    @property
    def geocoding_query(self) -> str:
        return f'{self.address}, {self.city}, {self.state}, USA'


@dataclass(frozen=True)
class StationLoadStats:
    rows_seen: int
    malformed_rows: int
    invalid_price_rows: int
    duplicate_station_ids: int
    duplicate_rows: int


@dataclass(frozen=True)
class StationDataset:
    stations: tuple[FuelStation, ...]
    stats: StationLoadStats
    coordinate_stats: dict[str, int] | None = None


def _normalize_text(value: str | None) -> str:
    return ' '.join((value or '').strip().split())


def _station_sort_key(station: FuelStation) -> tuple[str, ...]:
    return (
        str(station.retail_price),
        station.name.casefold(),
        station.address.casefold(),
        station.city.casefold(),
        station.state,
    )


class FuelStationRepository:
    """Loads, validates and deterministically deduplicates the local assessment CSV."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or settings.FUEL_STATION_CSV_PATH)

    def load(self, *, force_refresh: bool = False) -> StationDataset:
        if not self.path.is_file():
            raise StationDatasetError(f'Station dataset is missing: {self.path.name}.')
        stat = self.path.stat()
        signature = f'{self.path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'
        digest = hashlib.sha256(signature.encode()).hexdigest()
        cache_key = f'fuel-route:station-data:{digest}'
        cached = None if force_refresh else cache.get(cache_key)
        if cached is not None:
            return self._attach_stored_coordinates(cached)

        try:
            with self.path.open(encoding='utf-8-sig', newline='') as csv_file:
                result = self.parse_csv(csv_file)
        except StationDatasetError:
            raise
        except (OSError, csv.Error, UnicodeError) as exc:
            raise StationDatasetError('Station CSV could not be read safely.') from exc

        cache.set(cache_key, result, settings.STATION_DATA_CACHE_TTL)
        return self._attach_stored_coordinates(result)

    @staticmethod
    def _attach_stored_coordinates(dataset: StationDataset) -> StationDataset:
        """Attach only persisted coordinates for the exact current address fields."""
        from .models import StationCoordinate

        stored = {
            row.station_id: row
            for row in StationCoordinate.objects.all()
        }
        coordinate_stats = {
            'matched': 0,
            'no_match': 0,
            'failed': 0,
            'missing_or_stale': 0,
            'by_state': {},
        }
        for station in dataset.stations:
            state_stats = coordinate_stats['by_state'].setdefault(station.state, {
                'matched': 0, 'no_match': 0, 'failed': 0, 'missing_or_stale': 0,
            })
            record = stored.get(station.station_id)
            if record is None or record.input_hash != station.coordinate_input_hash:
                coordinate_stats['missing_or_stale'] += 1
                state_stats['missing_or_stale'] += 1
            elif record.status == StationCoordinate.MATCHED and record.latitude is not None and record.longitude is not None:
                coordinate_stats['matched'] += 1
                state_stats['matched'] += 1
            elif record.status == StationCoordinate.NO_MATCH:
                coordinate_stats['no_match'] += 1
                state_stats['no_match'] += 1
            else:
                coordinate_stats['failed'] += 1
                state_stats['failed'] += 1
        stations = tuple(
            replace(station, latitude=record.latitude, longitude=record.longitude)
            if (record := stored.get(station.station_id)) is not None
            and record.input_hash == station.coordinate_input_hash
            and record.status == StationCoordinate.MATCHED
            and record.latitude is not None and record.longitude is not None
            else station
            for station in dataset.stations
        )
        return replace(dataset, stations=stations, coordinate_stats=coordinate_stats)

    @staticmethod
    def parse_csv(csv_file: TextIO) -> StationDataset:
        """Parse a text stream; kept separate for deterministic in-memory testing."""
        reader = csv.DictReader(csv_file)
        columns = [column.strip() if column else '' for column in (reader.fieldnames or [])]
        missing_columns = set(REQUIRED_COLUMNS) - set(columns)
        if missing_columns:
            missing_text = ', '.join(sorted(missing_columns))
            raise StationDatasetError(f'Station CSV is missing required columns: {missing_text}.')
        reader.fieldnames = columns
        records: dict[str, list[FuelStation]] = {}
        rows_seen = malformed_rows = invalid_price_rows = 0

        for raw_row in reader:
            rows_seen += 1
            if None in raw_row:
                malformed_rows += 1
                continue
            row = {key.strip(): value for key, value in raw_row.items() if key is not None}
            if not any(_normalize_text(value) for value in row.values()):
                malformed_rows += 1
                continue

            station_id = _normalize_text(row.get('OPIS Truckstop ID')).casefold()
            name = _normalize_text(row.get('Truckstop Name'))
            address = _normalize_text(row.get('Address'))
            city = _normalize_text(row.get('City'))
            state = _normalize_text(row.get('State')).upper()
            if not all((station_id, name, address, city, state)):
                malformed_rows += 1
                continue

            price_text = _normalize_text(row.get('Retail Price'))
            try:
                price = Decimal(price_text)
                if not price.is_finite() or price <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                invalid_price_rows += 1
                continue

            station = FuelStation(station_id, name, address, city, state, price)
            records.setdefault(station_id, []).append(station)

        duplicate_ids = sum(len(stations) > 1 for stations in records.values())
        duplicate_rows = sum(len(stations) - 1 for stations in records.values() if len(stations) > 1)
        # Price snapshots sharing an ID collapse to the lowest valid price; ties use stable text fields.
        selected = tuple(
            min(stations, key=_station_sort_key)
            for _, stations in sorted(records.items())
        )
        return StationDataset(
            stations=selected,
            stats=StationLoadStats(
                rows_seen=rows_seen,
                malformed_rows=malformed_rows,
                invalid_price_rows=invalid_price_rows,
                duplicate_station_ids=duplicate_ids,
                duplicate_rows=duplicate_rows,
            ),
        )
