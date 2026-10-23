"""Строки відповіді в робочих днях (вихідні — субота й неділя)."""

from datetime import datetime, timedelta


def add_workdays(start: datetime, days: int) -> datetime:
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current
