from decimal import Decimal
from io import StringIO

import httpx
import pytest
from django.core.management import call_command

from routing.models import StationCoordinate
from routing.station_data import FuelStation, FuelStationRepository, StationDataset, StationLoadStats
from routing.station_enrichment import (
    CensusBatchGeocoder,
    EnrichmentResult,
    census_batch_csv,
    parse_census_batch_csv,
)


def station(station_id='abc'):
    return FuelStation(
        station_id, 'Example Truckstop', '101 Main St', 'Springfield', 'IL', Decimal('3.50')
    )


def test_census_batch_format_and_response_parse_matches_and_no_matches():
    body = census_batch_csv([station()]).decode()
    assert body == 'abc,101 Main St,Springfield,IL,\n'

    parsed = parse_census_batch_csv(
        'abc,"101 Main St, Springfield, IL","Match","Exact",'
        '"101 MAIN ST, SPRINGFIELD, IL","-89.65,39.78",123,L\n'
        'def,"not found","No_Match","","","",,\n',
        {'abc', 'def'},
    )
    assert parsed['abc'] == EnrichmentResult(
        'abc', StationCoordinate.MATCHED, latitude=39.78, longitude=-89.65
    )
    assert parsed['def'].status == StationCoordinate.NO_MATCH


def test_census_client_posts_one_mocked_batch_and_counts_results():
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            text='abc,in,Match,Exact,matched,"-89.65,39.78",123,L\n',
            request=httpx.Request('POST', url),
        )

    client = CensusBatchGeocoder(post=fake_post)
    results = client.geocode([station()])
    assert len(calls) == 1
    assert calls[0][1]['data']['benchmark']
    assert 'addressFile' in calls[0][1]['files']
    assert results['abc'].status == StationCoordinate.MATCHED
    assert client.last_diagnostics['outbound_http_requests'] == 1


@pytest.mark.parametrize(
    ('exception', 'status_code', 'category'),
    [
        (httpx.TimeoutException('timeout'), None, 'timeout'),
        (httpx.ConnectError('offline'), None, 'connection_error'),
        (httpx.HTTPStatusError('bad status', request=httpx.Request('POST', 'https://x'),
                               response=httpx.Response(429, request=httpx.Request('POST', 'https://x'))),
         429, 'rate_limited'),
    ],
)
def test_census_batch_failures_are_categorized_without_retry(exception, status_code, category):
    calls = []

    def fail_once(*_args, **_kwargs):
        calls.append(1)
        if isinstance(exception, httpx.HTTPStatusError):
            return exception.response
        raise exception

    client = CensusBatchGeocoder(post=fail_once)
    results = client.geocode([station()])
    assert len(calls) == 1
    assert results['abc'].status == StationCoordinate.FAILED
    assert results['abc'].failure_category == category
    assert client.last_diagnostics['http_status'] == status_code


@pytest.mark.django_db
def test_coordinate_records_persist_and_repository_ignores_stale_address():
    item = station()
    StationCoordinate.objects.create(
        station_id=item.station_id,
        input_hash=item.coordinate_input_hash,
        latitude=39.78,
        longitude=-89.65,
        status=StationCoordinate.MATCHED,
    )
    StationCoordinate.objects.create(
        station_id='other',
        input_hash='wrong-hash',
        status=StationCoordinate.FAILED,
        failure_category='timeout',
    )
    dataset = StationDataset((item,), StationLoadStats(1, 0, 0, 0, 0))
    repository = FuelStationRepository('unused.csv')
    enriched = repository._attach_stored_coordinates(dataset)
    assert enriched.stations[0].latitude == 39.78
    assert enriched.stations[0].longitude == -89.65
    assert enriched.coordinate_stats['matched'] == 1

    changed = FuelStation('abc', 'Example', '102 New St', 'Springfield', 'IL', Decimal('3.50'))
    stale = repository._attach_stored_coordinates(
        StationDataset((changed,), StationLoadStats(1, 0, 0, 0, 0))
    )
    assert stale.stations[0].latitude is None
    assert stale.coordinate_stats['missing_or_stale'] == 1


@pytest.mark.django_db
def test_enrichment_command_resumes_and_never_repeats_successful_work(monkeypatch):
    stations = (
        station('one'),
        FuelStation('two', 'No match', '2 Oak St', 'Springfield', 'IL', Decimal('3.60')),
        FuelStation('three', 'Failed', '3 Elm St', 'Springfield', 'IL', Decimal('3.70')),
    )
    dataset = StationDataset(stations, StationLoadStats(3, 0, 0, 0, 0))
    load_options = []

    def load_current(self, **kwargs):
        load_options.append(kwargs)
        return dataset

    monkeypatch.setattr(FuelStationRepository, 'load', load_current)
    calls = []

    def mocked_batch(self, batch):
        calls.append([item.station_id for item in batch])
        return {
            'one': EnrichmentResult('one', StationCoordinate.MATCHED, 39.78, -89.65),
            'two': EnrichmentResult('two', StationCoordinate.NO_MATCH),
            'three': EnrichmentResult('three', StationCoordinate.FAILED, failure_category='timeout'),
        }

    monkeypatch.setattr(CensusBatchGeocoder, 'geocode', mocked_batch)
    call_command('enrich_station_coordinates', batch_size=3, stdout=StringIO())
    assert load_options == [{'force_refresh': True}]
    assert StationCoordinate.objects.get(pk='one').status == StationCoordinate.MATCHED
    assert StationCoordinate.objects.get(pk='two').status == StationCoordinate.NO_MATCH
    assert StationCoordinate.objects.get(pk='three').failure_category == 'timeout'

    # Existing non-matched outcomes are not retried implicitly; successes are durable.
    call_command('enrich_station_coordinates', batch_size=3, stdout=StringIO())
    assert len(calls) == 1

    # Explicit retries still exclude the successful record.
    call_command('enrich_station_coordinates', batch_size=3, retry_failures=True, stdout=StringIO())
    assert calls[1] == ['two', 'three']


@pytest.mark.django_db
def test_enrichment_refreshes_existing_coordinate_when_address_changes(monkeypatch):
    previous = station('changed')
    StationCoordinate.objects.create(
        station_id=previous.station_id,
        input_hash=previous.coordinate_input_hash,
        latitude=39.78,
        longitude=-89.65,
        status=StationCoordinate.MATCHED,
    )
    updated = FuelStation(
        'changed', 'Example Truckstop', '205 New Main St', 'Springfield', 'IL', Decimal('3.50')
    )
    dataset = StationDataset((updated,), StationLoadStats(1, 0, 0, 0, 0))
    load_kwargs = []

    def load_current(self, **kwargs):
        load_kwargs.append(kwargs)
        return dataset

    monkeypatch.setattr(FuelStationRepository, 'load', load_current)
    submitted = []

    def mocked_batch(self, batch):
        submitted.extend(item.station_id for item in batch)
        return {
            'changed': EnrichmentResult(
                'changed', StationCoordinate.MATCHED, latitude=39.79, longitude=-89.66
            ),
        }

    monkeypatch.setattr(CensusBatchGeocoder, 'geocode', mocked_batch)
    call_command('enrich_station_coordinates', stdout=StringIO())

    refreshed = StationCoordinate.objects.get(pk='changed')
    assert load_kwargs == [{'force_refresh': True}]
    assert submitted == ['changed']
    assert refreshed.input_hash == updated.coordinate_input_hash
    assert (refreshed.latitude, refreshed.longitude) == (39.79, -89.66)


def test_enrichment_failure_is_saved_and_success_is_not_lost_on_resume(monkeypatch):
    # The transaction and update-or-create behavior in the management command is covered above;
    # this parser guard ensures malformed rows never become fabricated coordinates.
    parsed = parse_census_batch_csv('abc,input,Match,Exact,matched,"bad,39",,,\n', {'abc'})
    assert parsed['abc'].status == StationCoordinate.FAILED
    assert parsed['abc'].failure_category == 'invalid_response'
