import logging
from time import perf_counter

from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .clients.exceptions import (
    AddressNotFound,
    LocationNotInUSA,
    NoRouteFound,
    UpstreamError,
    UpstreamTimeout,
)
from .serializers import RoutePlanRequestSerializer
from .services import RoutePlanningService
from .station_data import StationDatasetError

logger = logging.getLogger(__name__)


def route_map(request):
    """Serve a lightweight Leaflet page for the route-planning API."""
    return render(request, 'routing/map.html')


class RoutePlanView(APIView):
    """Resolve endpoints and return a driving route with a bounded fuel-stop plan."""

    def post(self, request):
        request_started = perf_counter()
        response = None
        try:
            serializer = RoutePlanRequestSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            try:
                result = RoutePlanningService().plan(serializer.validated_data)
            except (AddressNotFound, LocationNotInUSA) as exc:
                response = Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except NoRouteFound as exc:
                response = Response({'detail': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            except UpstreamTimeout as exc:
                response = Response({'detail': str(exc)}, status=status.HTTP_504_GATEWAY_TIMEOUT)
            except UpstreamError as exc:
                body = {'detail': str(exc)}
                if exc.failure_category == 'rate_limited':
                    body['error_code'] = 'rate_limited'
                response = Response(body, status=status.HTTP_502_BAD_GATEWAY)
            except StationDatasetError as exc:
                response = Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            except Exception:
                logger.exception('Unexpected route-plan failure.')
                response = Response(
                    {'detail': 'An unexpected error occurred while creating the route plan.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            else:
                response = Response(result, status=status.HTTP_200_OK)
            return response
        finally:
            elapsed_ms = round((perf_counter() - request_started) * 1000, 3)
            if response is not None and response.status_code == status.HTTP_200_OK:
                response.data['diagnostics']['timings_ms']['total_request_processing'] = elapsed_ms
            logger.info('Route-plan HTTP processing: duration_ms=%.3f', elapsed_ms)

# Create your views here.
