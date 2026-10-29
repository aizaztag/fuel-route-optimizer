class UpstreamError(Exception):
    """An external service failed or returned an invalid response."""

    def __init__(
        self,
        message: str,
        *,
        failure_category: str = 'upstream_error',
        failure_source: str = 'response',
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.failure_source = failure_source
        self.http_status = http_status


class UpstreamTimeout(UpstreamError):
    """An external service did not respond within its configured timeout."""


class AddressNotFound(Exception):
    """No US address match was returned by the geocoder."""


class LocationNotInUSA(Exception):
    """The geocoder could not verify a location as being in the United States."""


class NoRouteFound(Exception):
    """OSRM could not find a driving route between the requested points."""
