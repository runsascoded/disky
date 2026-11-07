import re
from re import fullmatch, IGNORECASE

from click import Context, Parameter

INT_RGX = re.compile(r'(?P<base>[\d.]+)(?P<suffix>[kmb]i?)', flags=IGNORECASE)
ORDERS_ISO = { 'k': 2**10, 'm': 2**20, 'b': 2**30 }
ORDERS_IEC = { 'k': 1e3  , 'm': 1e6  , 'b': 1e9   }


def parse_int(ctx: Context, param: Parameter, value: str) -> int:
    m = fullmatch(INT_RGX, value.lower())
    if m:
        base = float(m['base'])
        suffix = m['suffix']
        order: float
        if suffix.endswith('i'):
            suffix = suffix[:-1]
            order = ORDERS_ISO[suffix]
        else:
            order = ORDERS_IEC[suffix]
        n = int(base * order)
    else:
        n = int(float(value))
    return n
