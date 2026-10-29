import logging

from time import perf_counter
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import StationCoordinate
from routing.station_data import FuelStationRepository
from routing.station_enrichment import CensusBatchGeocoder

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Batch-enrich fuel-station coordinates once and persist results locally.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=1000,
            help='Records per Census batch (1 to 10,000; default 1,000).',
        )
        parser.add_argument(
            '--retry-failures', action='store_true',
            help='Retry previously failed or unmatched records; successful work is always skipped.',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if not 1 <= batch_size <= 10000:
            raise CommandError('--batch-size must be between 1 and 10000.')

        # Always inspect the current source contents, even when a file replacement
        # preserved both size and modification timestamp.
        dataset = FuelStationRepository().load(force_refresh=True)
        rows = {
            row.station_id: row
            for row in StationCoordinate.objects.all()
        }
        pending = []
        for station in dataset.stations:
            current = rows.get(station.station_id)
            same_input = current is not None and current.input_hash == station.coordinate_input_hash
            if same_input and current.status == StationCoordinate.MATCHED:
                continue
            if same_input and not options['retry_failures']:
                continue
            pending.append(station)

        self.stdout.write(f'{len(pending)} stations queued; successful coordinates will be reused.')
        geocoder = CensusBatchGeocoder()
        matched = no_match = failed = submitted = 0
        request_failures = 0
        batch_count = 0
        enrichment_started = perf_counter()
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            # The official browser interface also imposes a 5 MB batch-file ceiling.
            # Keep comfortably below it rather than risking silent provider rejection.
            from routing.station_enrichment import census_batch_csv
            if len(census_batch_csv(batch)) > 4_500_000:
                raise CommandError('Batch file exceeds the safe 4.5 MB limit; reduce --batch-size.')
            results = geocoder.geocode(batch)
            submitted += len(batch)
            batch_count += 1
            categories = geocoder.last_diagnostics.get('failure_categories', {})
            batch_failed = bool(set(categories) & {
                'timeout', 'connection_error', 'http_error', 'rate_limited', 'invalid_response'
            })
            if batch_failed:
                request_failures += 1
            with transaction.atomic():
                for station in batch:
                    result = results[station.station_id]
                    StationCoordinate.objects.update_or_create(
                        station_id=station.station_id,
                        defaults={
                            'input_hash': station.coordinate_input_hash,
                            'latitude': result.latitude,
                            'longitude': result.longitude,
                            'status': result.status,
                            'failure_category': result.failure_category,
                            'provider': 'us_census_geocoder',
                        },
                    )
                    if result.status == StationCoordinate.MATCHED:
                        matched += 1
                    elif result.status == StationCoordinate.NO_MATCH:
                        no_match += 1
                    else:
                        failed += 1
            self.stdout.write(
                f'Batch {offset // batch_size + 1}: {len(batch)} submitted, '
                f'{sum(item.status == StationCoordinate.MATCHED for item in results.values())} matched.'
            )
            if batch_failed:
                # Persist this result and leave later records untouched for safe resumption.
                break

        logger.info(
            'Station enrichment complete: submitted=%d matched=%d no_match=%d failed=%d '
            'http_batches=%d failed_batches=%d duration_ms=%.3f',
            submitted, matched, no_match, failed, batch_count,
            request_failures,
            (perf_counter() - enrichment_started) * 1000,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Finished: {submitted} submitted, {matched} matched, '
                f'{no_match} unmatched, {failed} failed.'
            )
        )
        if request_failures:
            raise CommandError(
                f'{request_failures} batch request(s) failed; their statuses are persisted. '
                'Use --retry-failures to retry those records.'
            )
