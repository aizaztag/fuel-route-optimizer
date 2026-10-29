import csv
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


DATASET_PATH = Path(__file__).resolve().parents[3] / 'fuel-prices-for-be-assessment.csv'
STATION_ID_COLUMN = 'OPIS Truckstop ID'
PRICE_COLUMN = 'Retail Price'
STATE_COLUMN = 'State'


class Command(BaseCommand):
    help = 'Inspect the provided fuel station CSV without modifying it.'

    def handle(self, *args, **options):
        if not DATASET_PATH.is_file():
            raise CommandError(
                f'Fuel dataset not found at {DATASET_PATH}. '
                'Place fuel-prices-for-be-assessment.csv in the project root.'
            )

        try:
            with DATASET_PATH.open(encoding='utf-8-sig', newline='') as csv_file:
                reader = csv.DictReader(csv_file)
                columns = reader.fieldnames or []
                rows = list(reader)
        except (OSError, csv.Error, UnicodeError) as exc:
            raise CommandError(f'Unable to read fuel dataset: {exc}') from exc

        missing = {
            column: sum(not (row.get(column) or '').strip() for row in rows)
            for column in columns
        }
        station_ids = Counter(
            (row.get(STATION_ID_COLUMN) or '').strip()
            for row in rows
            if (row.get(STATION_ID_COLUMN) or '').strip()
        )
        repeated_station_ids = {
            station_id: count
            for station_id, count in station_ids.items()
            if count > 1
        }
        exact_rows = Counter(tuple(row.get(column, '') for column in columns) for row in rows)
        repeated_row_groups = sum(count > 1 for count in exact_rows.values())
        extra_exact_rows = sum(count - 1 for count in exact_rows.values() if count > 1)

        prices = []
        invalid_price_rows = []
        for row_number, row in enumerate(rows, start=2):
            raw_price = (row.get(PRICE_COLUMN) or '').strip()
            if not raw_price:
                continue  # Missing values are reported separately.
            try:
                price = Decimal(raw_price)
                if not price.is_finite() or price < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                invalid_price_rows.append((row_number, raw_price))
            else:
                prices.append(price)

        sorted_prices = sorted(prices)
        median = None
        if sorted_prices:
            middle = len(sorted_prices) // 2
            median = (
                sorted_prices[middle]
                if len(sorted_prices) % 2
                else (sorted_prices[middle - 1] + sorted_prices[middle]) / Decimal(2)
            )

        state_counts = Counter(
            (row.get(STATE_COLUMN) or '').strip() or '(missing)'
            for row in rows
        )
        coordinate_headers = [
            column
            for column in columns
            if any(token in column.lower().replace(' ', '_') for token in ('latitude', 'longitude', 'lat', 'lon'))
        ]

        self.stdout.write(f'CSV: {DATASET_PATH}')
        self.stdout.write(f'Row count: {len(rows)}')
        self.stdout.write(f'Columns ({len(columns)}): {columns}')
        self.stdout.write(f'Missing values by column: {missing}')
        self.stdout.write(f'Invalid retail prices: {len(invalid_price_rows)}')
        if invalid_price_rows:
            self.stdout.write(f'Invalid price rows (CSV line, value): {invalid_price_rows}')
        self.stdout.write(
            f'Duplicate station IDs: {len(repeated_station_ids)} IDs; '
            f'{sum(repeated_station_ids.values())} rows involved; '
            f'{sum(count - 1 for count in repeated_station_ids.values())} extra rows'
        )
        self.stdout.write(f'Repeated station IDs (ID: row count): {repeated_station_ids}')
        self.stdout.write(
            f'Exact duplicate rows: {repeated_row_groups} repeated row groups; '
            f'{extra_exact_rows} extra duplicate rows'
        )
        self.stdout.write(
            'Retail price statistics: '
            f'min={min(prices) if prices else "n/a"}, '
            f'max={max(prices) if prices else "n/a"}, '
            f'median={median if median is not None else "n/a"}'
        )
        self.stdout.write(f'Counts by state: {dict(sorted(state_counts.items()))}')
        if coordinate_headers:
            self.stdout.write(f'Station coordinate columns present: {coordinate_headers}')
        else:
            self.stdout.write('Station coordinates are not present in the CSV.')
