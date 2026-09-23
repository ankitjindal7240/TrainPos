"""Resolve the shared email parser selected by a tenant-owned Vendor."""

from Orders.models import Vendor
from Orders.parsers.demo import parse_demo_email
from Orders.parsers.homebytes import parse_homebytes_email
from Orders.parsers.railrecipe import parse_railrecipe_email
from Orders.parsers.railrestro import parse_railrestro_email
from Orders.parsers.rajbhog_khana import parse_rajbhog_khana_email


PARSERS = {
    Vendor.ParserType.DEMO: parse_demo_email,
    Vendor.ParserType.RAILRESTRO: parse_railrestro_email,
    Vendor.ParserType.HOMEBYTES: parse_homebytes_email,
    Vendor.ParserType.RAJBHOG: parse_rajbhog_khana_email,
    Vendor.ParserType.RAILRECIPE: parse_railrecipe_email,
}


def get_vendor_parser(vendor):
    try:
        return PARSERS[vendor.parser_type]
    except KeyError as error:
        raise ValueError("Vendor has no supported parser configured.") from error
