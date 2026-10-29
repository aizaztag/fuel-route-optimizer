import httpx
import pytest
from decimal import Decimal
from django.core.cache import cache
from django.test import override_settings

import routing.clients.geocoding as geocoding
from routing.clients.geocoding import NominatimClient
from routing.clients.exceptions import AddressNotFound, LocationNotInUSA, UpstreamError, UpstreamTimeout
from routing.station_data import FuelStation


@pytest.fixture(autouse=True)
def clear_cache_and_rate_state():
    cache.clear()
    with geocoding._nominatim_lock:
        geocoding._last_nominatim_request_at = 0.0


def response(payload):
    return httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://test.invalid'))


def test_successful_station_geocoding_is_cached(monkeypatch):
    calls = []
    station = FuelStation(
        '101', 'Test Travel Stop', 'I-90, EXIT 2', 'Example City', 'NY',
        Decimal('3.25'),
    )

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response([{
            'lat': '40.7', 'lon': '-74.0',
            'address': {
                'country_code': 'us', 'state': 'New York', 'city': 'New York',
                'ISO3166-2-lvl4': 'US-NY',
            },
        }])

    monkeypatch.setattr(httpx, 'get', fake_get)
    with override_settings(NOMINATIM_MIN_INTERVAL_SECONDS=0):
        client = NominatimClient()
        first = client.geocode_us_place(station.geocoding_query)
        second = client.geocode_us_place(station.geocoding_query)

    assert first == second
    assert first['state_code'] == 'NY'
    assert first['city'] == 'New York'
    assert calls[0][1]['params']['q'] == (
        'I-90, EXIT 2, Example City, NY, USA'
    )
    assert len(calls) == 1
    assert [event['cache_status'] for event in client.lookup_events] == ['miss', 'hit']
    assert client.lookup_events[0]['outbound_http_request'] is True
    assert client.lookup_events[1]['outbound_http_request'] is False
    assert client.lookup_events[0]['http_status'] == 200
    assert client.lookup_events[0]['duration_ms'] >= 0
    assert client.lookup_events[1]['http_status'] is None
    assert client.lookup_events[1]['failed'] is False
    assert calls[0][1]['headers']['User-Agent'] == geocoding.settings.NOMINATIM_USER_AGENT
    assert calls[0][1]['timeout'] == geocoding.settings.NOMINATIM_TIMEOUT_SECONDS


def test_nominatim_requests_are_spaced_by_configured_interval(monkeypatch):
    now = [100.0]
    sleeps = []
    calls = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def fake_get(url, **kwargs):
        calls.append(url)
        return response([{
            'lat': '40.7', 'lon': '-74.0',
            'address': {'country_code': 'us', 'state': 'New York'},
        }])

    monkeypatch.setattr(httpx, 'get', fake_get)
    with override_settings(NOMINATIM_MIN_INTERVAL_SECONDS=1):
        client = NominatimClient(monotonic=lambda: now[0], sleeper=fake_sleep)
        client.geocode_us_place('first place')
        client.geocode_us_place('second place')

    assert calls == [geocoding.settings.NOMINATIM_SEARCH_URL] * 2
    assert sleeps == [pytest.approx(1.0)]


def test_not_found_location_is_negatively_cached(monkeypatch):
    calls = []
    station = FuelStation(
        '102', 'Unmapped Travel Stop', 'I-80 EXIT 1', 'Nowhere', 'NY',
        Decimal('3.50'),
    )

    def fake_get(url, **kwargs):
        calls.append(url)
        return response([])

    monkeypatch.setattr(httpx, 'get', fake_get)
    with override_settings(NOMINATIM_MIN_INTERVAL_SECONDS=1):
        client = NominatimClient()
        with pytest.raises(AddressNotFound):
            client.geocode_us_place(station.geocoding_query)
        with pytest.raises(AddressNotFound):
            client.geocode_us_place(station.geocoding_query)

    assert calls == [geocoding.settings.NOMINATIM_SEARCH_URL]
    assert [event['cache_status'] for event in client.lookup_events] == ['miss', 'hit']
    assert all(event['failed'] for event in client.lookup_events)
    assert [event['failure_category'] for event in client.lookup_events] == ['empty_result', 'empty_result']
    assert client.lookup_events[0]['failure_source'] == 'lookup_result'
    assert client.lookup_events[0]['http_status'] == 200
    assert client.lookup_events[1]['outbound_http_request'] is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    ('failure', 'expected_category', 'expected_source', 'expected_status'),
    [
        (httpx.ReadTimeout('timeout'), 'timeout', 'transport', None),
        (httpx.ConnectError('connection'), 'connection_error', 'transport', None),
        (httpx.RemoteProtocolError('protocol'), 'transport_error', 'transport', None),
    ],
)
def test_transport_failure_categories_are_sanitized_and_not_retried(
    monkeypatch, failure, expected_category, expected_source, expected_status
):
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = NominatimClient(sleeper=lambda _seconds: None)
    with pytest.raises((UpstreamTimeout, UpstreamError)):
        client.geocode_us_place('Private address must not appear in diagnostics')

    event = client.lookup_events[0]
    assert len(calls) == 1
    assert event['failure_category'] == expected_category
    assert event['failure_source'] == expected_source
    assert event['http_status'] == expected_status
    assert isinstance(event['duration_ms'], float)
    assert 'Private address' not in repr(event)


@pytest.mark.parametrize(
    ('status_code', 'category'), [(429, 'rate_limited'), (503, 'http_error')]
)
def test_http_failures_are_distinguished_without_retry(monkeypatch, status_code, category):
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return httpx.Response(
            status_code, request=httpx.Request('GET', 'https://test.invalid')
        )

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = NominatimClient(sleeper=lambda _seconds: None)
    with pytest.raises(UpstreamError):
        client.geocode_us_place('Some address')

    assert len(calls) == 1
    assert client.lookup_events[0]['failure_category'] == category
    assert client.lookup_events[0]['failure_source'] == 'http'
    assert client.lookup_events[0]['http_status'] == status_code


def test_invalid_json_and_invalid_shape_are_invalid_responses(monkeypatch):
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return httpx.Response(
            200, content=b'not-json', request=httpx.Request('GET', 'https://test.invalid')
        )

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = NominatimClient(sleeper=lambda _seconds: None)
    with pytest.raises(UpstreamError):
        client.geocode_us_place('Malformed response address')
    assert client.lookup_events[-1]['failure_category'] == 'invalid_response'
    assert client.lookup_events[-1]['http_status'] == 200

    monkeypatch.setattr(httpx, 'get', lambda *_args, **_kwargs: response({'unexpected': 'shape'}))
    with pytest.raises(UpstreamError):
        client.geocode_us_place('Different malformed response address')
    assert client.lookup_events[-1]['failure_category'] == 'invalid_response'
    assert len(calls) == 1


def test_non_us_result_category_is_negative_cached(monkeypatch):
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return response([{
            'lat': '45', 'lon': '-70',
            'address': {'country_code': 'ca', 'state': 'Quebec'},
        }])

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = NominatimClient(sleeper=lambda _seconds: None)
    with pytest.raises(LocationNotInUSA):
        client.geocode_us_place('A location outside the USA')
    with pytest.raises(LocationNotInUSA):
        client.geocode_us_place('A location outside the USA')

    assert len(calls) == 1
    assert [event['failure_category'] for event in client.lookup_events] == ['non_us', 'non_us']
    assert [event['cache_status'] for event in client.lookup_events] == ['miss', 'hit']
    assert client.lookup_events[0]['failure_source'] == 'lookup_result'
    assert client.lookup_events[1]['outbound_http_request'] is False


def test_forward_geocoding_cache_key_changes_with_service_endpoint(monkeypatch):
    now = [100.0]
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        latitude = '40.7' if len(calls) == 1 else '40.8'
        return response([{
            'lat': latitude,
            'lon': '-74.0',
            'address': {'country_code': 'us', 'state': 'New York'},
        }])

    monkeypatch.setattr(httpx, 'get', fake_get)
    client = NominatimClient(monotonic=lambda: now[0], sleeper=lambda delay: now.__setitem__(0, now[0] + delay))
    first = client.geocode_us_place('same address')
    with override_settings(NOMINATIM_SEARCH_URL='https://nominatim.example/custom/search'):
        second = client.geocode_us_place('same address')

    assert first['coordinates']['latitude'] == 40.7
    assert second['coordinates']['latitude'] == 40.8
    assert calls == [
        geocoding.settings.NOMINATIM_SEARCH_URL,
        'https://nominatim.example/custom/search',
    ]
