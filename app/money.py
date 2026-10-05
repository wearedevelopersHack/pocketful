"""Money at the client edge: text <-> integer minor units. The float boundary.

Two rules from INVARIANTS §1 / §5 and plan §2.3.5 govern this module:

1. Money that is sent or stored is an **integer count of minor units**. This is
   the only place user text becomes money, and it becomes an ``int`` here — no
   ``float``, no ``Decimal``, no ``parseFloat``/``toFixed`` analogue ever touches
   a value that is sent.
2. A value with more than two decimal places is **rejected locally**, not
   silently rounded. Rounding a user's input is the client inventing or
   destroying a cent, which is exactly the class of bug this build exists to
   make impossible.

Display formatting (integer cents -> ``"$12.34"``) also lives here, and is
integer arithmetic (``divmod``) end to end. It is applied only at the render
edge, never to a value on its way to the wire.
"""

from __future__ import annotations

# The wire/storage bound, mirroring plan §1: +-(2**53 - 1). Defined locally so
# the client stays a standalone HTTP consumer that does not import the ledger.
MAX_MINOR = 2**53 - 1

_DIGITS = "0123456789"

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


class InvalidMoneyInput(ValueError):
    """User input that cannot be represented exactly in minor units."""


def _is_digits(text: str) -> bool:
    return len(text) > 0 and all(ch in _DIGITS for ch in text)


def parse_amount_to_minor(text: object, currency: str = "USD") -> int:
    """Parse user-entered money text into an integer count of minor units.

    Accepts ``"12"``, ``"12.3"``, ``"12.34"`` and an optional leading ``$``.
    Rejects (raises :class:`InvalidMoneyInput`): non-text, empty, signed,
    thousands separators, non-numeric, more than two decimal places, zero,
    negative, and anything above :data:`MAX_MINOR`. Never rounds, never floats.
    """
    if not isinstance(text, str):
        raise InvalidMoneyInput(f"amount must be text, got {type(text).__name__}")
    raw = text.strip()
    if raw.startswith("$"):
        raw = raw[1:].strip()
    if raw == "":
        raise InvalidMoneyInput("amount is empty")
    if raw[0] in "+-":
        raise InvalidMoneyInput("amount must be positive")
    if "," in raw:
        raise InvalidMoneyInput("no thousands separators; digits and one '.' only")

    whole, dot, frac = raw.partition(".")
    if not _is_digits(whole):
        raise InvalidMoneyInput(f"not a number: {text!r}")
    if dot:
        if not _is_digits(frac):
            raise InvalidMoneyInput(f"not a number: {text!r}")
        if len(frac) > 2:
            raise InvalidMoneyInput(
                "more than two decimal places is not representable in minor units")
        if len(frac) == 1:
            frac = frac + "0"
    else:
        frac = "00"

    # Integer arithmetic only: dollars * 100 + cents. No float step exists here.
    minor = int(whole) * 100 + int(frac)
    if minor <= 0:
        raise InvalidMoneyInput("amount must be greater than zero")
    if minor > MAX_MINOR:
        raise InvalidMoneyInput(f"amount exceeds the maximum of {MAX_MINOR} minor units")
    return minor


def format_minor(amount_minor: int, currency: str = "USD") -> str:
    """Format integer minor units for display. Render edge only.

    Integer ``divmod`` throughout — a negative balance (overdraft) keeps its
    sign, and there is no float to drift.
    """
    if type(amount_minor) is not int:
        raise TypeError(
            f"amount_minor must be an int (minor units), got {type(amount_minor).__name__}")
    # U+2212 MINUS SIGN, not the ASCII hyphen. One minus in the product: the
    # activity table signs a debit with U+2212 (app/design.py), and this is the
    # balance formatter, so an ASCII hyphen here put two different minus
    # characters in the same document — visible on the one page whose balance can
    # be negative (the system account's, whose activity rows are all `−`-signed).
    # Recorded counter-argument, not dismissed: an ASCII hyphen is announced as
    # "minus" by more screen readers than U+2212 is. Consistency across one page
    # wins here; if that trade is ever revisited, this line and the activity
    # table change together, and DESIGN-SPEC §5.6's table changes with them.
    sign = "−" if amount_minor < 0 else ""
    whole, frac = divmod(abs(amount_minor), 100)
    symbol = _SYMBOLS.get(currency)
    if symbol is not None:
        return f"{sign}{symbol}{whole}.{frac:02d}"
    return f"{sign}{whole}.{frac:02d} {currency}"
