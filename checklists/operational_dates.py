from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone


OPERATIONAL_ENTRY_LOOKBACK_DAYS = 7


def operational_entry_bounds(today=None):
    today = today or timezone.localdate()
    return today - timedelta(days=OPERATIONAL_ENTRY_LOOKBACK_DAYS), today


def validate_operational_entry_date(operational_date, today=None):
    earliest, latest = operational_entry_bounds(today)
    if operational_date < earliest or operational_date > latest:
        raise ValidationError(
            "Chore List entry is available only from "
            f"{earliest.isoformat()} through {latest.isoformat()}."
        )
    return operational_date
