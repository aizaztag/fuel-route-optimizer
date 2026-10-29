from rest_framework import serializers


class CoordinateSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()

    def to_internal_value(self, data):
        if hasattr(data, 'keys'):
            unknown = set(data.keys()) - {'latitude', 'longitude'}
            if unknown:
                raise serializers.ValidationError({
                    field: ['Unknown field.'] for field in sorted(unknown)
                })
        return super().to_internal_value(data)

    def validate_latitude(self, value: float) -> float:
        if not -90 <= value <= 90:
            raise serializers.ValidationError('Must be between -90 and 90.')
        return value

    def validate_longitude(self, value: float) -> float:
        if not -180 <= value <= 180:
            raise serializers.ValidationError('Must be between -180 and 180.')
        return value


class RoutePlanRequestSerializer(serializers.Serializer):
    start_address = serializers.CharField(max_length=500, required=False, allow_blank=False)
    finish_address = serializers.CharField(max_length=500, required=False, allow_blank=False)
    start = CoordinateSerializer(required=False)
    finish = CoordinateSerializer(required=False)

    def to_internal_value(self, data):
        if hasattr(data, 'keys'):
            allowed = {'start_address', 'finish_address', 'start', 'finish'}
            unknown = set(data.keys()) - allowed
            if unknown:
                raise serializers.ValidationError({
                    field: ['Unknown field.'] for field in sorted(unknown)
                })
        return super().to_internal_value(data)

    def validate(self, attrs):
        address_mode = 'start_address' in attrs or 'finish_address' in attrs
        coordinate_mode = 'start' in attrs or 'finish' in attrs

        if address_mode and coordinate_mode:
            raise serializers.ValidationError(
                'Provide either both addresses or both coordinate objects, not a mixture.'
            )
        if address_mode:
            missing = [key for key in ('start_address', 'finish_address') if key not in attrs]
            if missing:
                raise serializers.ValidationError({key: ['This field is required.'] for key in missing})
        elif coordinate_mode:
            missing = [key for key in ('start', 'finish') if key not in attrs]
            if missing:
                raise serializers.ValidationError({key: ['This field is required.'] for key in missing})
        else:
            raise serializers.ValidationError(
                'Provide start_address and finish_address, or start and finish coordinates.'
            )
        return attrs
