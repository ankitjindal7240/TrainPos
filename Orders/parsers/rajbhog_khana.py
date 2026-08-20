from Orders.parsers.homebytes import parse_homebytes_email


def parse_rajbhog_khana_email(body):
    """Return V1 receipt fields from a Rajbhog Khana invoice email.

    Rajbhog Khana's observed emails use the same invoice-table layout as
    HomeBytes, while retaining their own RBK invoice numbers and payment data.
    """
    return parse_homebytes_email(body)
