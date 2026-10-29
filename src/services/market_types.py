"""Explicit Shanghai timestamps, integer fen and conservative platform status mapping."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def now():
    return datetime.now(TZ)


def stamp(value=None):
    value = value or now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ).isoformat(timespec='seconds')


def daily_at(value, clock, *, tomorrow=False):
    value = datetime.fromisoformat(stamp(value))
    result = datetime.combine(value.date(), time.fromisoformat(clock), TZ)
    if tomorrow or result <= value:
        result += timedelta(days=1)
    return stamp(result)


def cents(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace('¥', '').replace('￥', '').replace(',', '')
    multiplier = 10000 if text.endswith('万') else 1
    text = text.removesuffix('万')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', text):
        return None
    try:
        amount = Decimal(text) * multiplier * 100
        return int(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if 0 < amount <= 10**11 else None
    except InvalidOperation:
        return None


def platform_status(raw):
    # Only observed legacy contract codes are mapped; unknown codes stay unknown.
    if isinstance(raw, bool):
        return 'unknown'
    return {'0': 'active', '1': 'sold'}.get(str(raw), 'unknown')


@dataclass
class SellerListing:
    items: list[dict] = field(default_factory=list)
    request_ok: bool = False
    page_complete: bool = False
    list_complete: bool = False
    error: str | None = None
    source: str = 'seller_item_list'


class PlatformAccessError(Exception):
    """Only safe error codes, never cookies, tokens or raw response bodies."""


class LoginExpired(PlatformAccessError):
    pass


class AccessRestricted(PlatformAccessError):
    pass


class LeaseLost(Exception):
    pass
