from django.test import Client


def test_homepage_serves_leaflet_route_map_connected_to_api():
    response = Client().get('/')

    assert response.status_code == 200
    assert b'leaflet' in response.content.lower()
    assert b'/api/v1/route-plan/' in response.content
    assert b'https://tile.openstreetmap.org/{z}/{x}/{y}.png' in response.content
    assert b'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' in response.content
    assert b'<meta name="referrer" content="strict-origin-when-cross-origin">' in response.content
    assert b'fuel_stops' in response.content
    assert b'optimization_status' in response.content
    assert b'vehicle starts with a full tank' in response.content.lower()


def test_map_response_permits_origin_referrer_and_is_not_forced_uncacheable():
    response = Client(HTTP_HOST='127.0.0.1').get('/')

    assert response.status_code == 200
    assert response['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert 'Cache-Control' not in response or 'no-cache' not in response['Cache-Control'].lower()
