from django.db import models


class StationCoordinate(models.Model):
    """Persistent result of the explicit offline station-enrichment command."""

    MATCHED = 'matched'
    NO_MATCH = 'no_match'
    FAILED = 'failed'
    STATUS_CHOICES = (
        (MATCHED, 'Matched'),
        (NO_MATCH, 'No match'),
        (FAILED, 'Failed'),
    )

    station_id = models.CharField(max_length=64, primary_key=True)
    input_hash = models.CharField(max_length=64, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, db_index=True)
    failure_category = models.CharField(max_length=32, blank=True)
    provider = models.CharField(max_length=32, default='us_census_geocoder')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=('status', 'input_hash'))]
