import csv
import os
from pathlib import Path
from uuid import uuid4
from io import StringIO
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache

from routing.models import StationCoordinate
from routing.station_data import FuelStationRepository, StationDatasetError
from routing.station_data import FuelStation

COLUMNS = [
    'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City', 'State', 'Rack ID', 'Retail Price',
]


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def csv_text(rows, columns=COLUMNS):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(column, '') for column in columns])
    return output.getvalue()


def station_row(station_id='101', price='3.25', name='  Test   Travel Stop '):
    return {
        'OPIS Truckstop ID': station_id,
        'Truckstop Name': name,
        'Address': ' I-90, EXIT 2 ',
        'City': '  Example City ',
        'State': 'ny',
        'Rack ID': '3',
        'Retail Price': price,
    }


def test_load_normalizes_and_deterministically_collapses_duplicate_ids():
    csv_file = StringIO(csv_text([
        station_row(' 101 ', '3.50', 'Stop B'),
        station_row('101', '3.25', ' Stop A '),
    ]))

    result = FuelStationRepository.parse_csv(csv_file)

    assert len(result.stations) == 1
    assert result.stations[0].station_id == '101'
    assert result.stations[0].name == 'Stop A'
    assert result.stations[0].city == 'Example City'
    assert result.stations[0].state == 'NY'
    assert result.stations[0].retail_price == Decimal('3.25')
    assert result.stats.duplicate_station_ids == 1
    assert result.stats.duplicate_rows == 1


def test_malformed_rows_are_skipped_without_corrupting_valid_rows():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    writer.writerow([station_row().get(column, '') for column in COLUMNS])
    writer.writerow(['too', 'few', 'columns'])
    writer.writerow([station_row().get(column, '') for column in COLUMNS] + ['unexpected'])
    result = FuelStationRepository.parse_csv(StringIO(output.getvalue()))

    assert len(result.stations) == 1
    assert result.stats.rows_seen == 3
    assert result.stats.malformed_rows == 2


def test_missing_required_columns_raise_clear_dataset_error():
    with pytest.raises(StationDatasetError, match='missing required columns'):
        FuelStationRepository.parse_csv(StringIO(csv_text([], columns=COLUMNS[:-1])))


def test_non_numeric_zero_and_negative_prices_are_skipped():
    csv_file = StringIO(csv_text([
        station_row('1', 'not-a-price'),
        station_row('2', '0'),
        station_row('3', '-1'),
        station_row('4', '2.50'),
    ]))

    result = FuelStationRepository.parse_csv(csv_file)

    assert [station.station_id for station in result.stations] == ['4']
    assert result.stats.invalid_price_rows == 3


def test_empty_dataset_returns_empty_station_list():
    result = FuelStationRepository.parse_csv(StringIO(csv_text([])))
    assert result.stations == ()
    assert result.stats.rows_seen == 0


def test_station_geocoding_query_uses_address_city_state_and_country():
    station = FuelStation(
        '101', 'Test Travel Stop', 'I-90, EXIT 2', 'Example City', 'NY', Decimal('3.25')
    )

    assert station.geocoding_query == (
        'I-90, EXIT 2, Example City, NY, USA'
    )


@pytest.mark.django_db
def test_force_refresh_reads_updated_price_and_reuses_current_coordinates():
    cache.clear()
    path = Path(settings.BASE_DIR) / f'.station-refresh-test-{uuid4().hex}.csv'
    try:
        path.write_text(csv_text([station_row('station-a', '3.25')]), encoding='utf-8')
        repository = FuelStationRepository(path)
        initial = repository.load(force_refresh=True).stations[0]
        StationCoordinate.objects.create(
            station_id=initial.station_id,
            input_hash=initial.coordinate_input_hash,
            latitude=41.5,
            longitude=-87.5,
            status=StationCoordinate.MATCHED,
        )
        original_stat = path.stat()

        # Same-length price replacement with the original timestamp exercises the
        # explicit refresh path rather than relying on the stat-based cache key.
        path.write_text(csv_text([station_row('station-a', '4.75')]), encoding='utf-8')
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        refreshed = repository.load(force_refresh=True).stations[0]

        assert refreshed.retail_price == Decimal('4.75')
        assert (refreshed.latitude, refreshed.longitude) == (41.5, -87.5)
        # A subsequent request in this process uses the refreshed parsed snapshot.
        assert repository.load().stations[0].retail_price == Decimal('4.75')
    finally:
        path.unlink(missing_ok=True)
        cache.clear()
