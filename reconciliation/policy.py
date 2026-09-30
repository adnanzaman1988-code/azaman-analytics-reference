"""Matching tolerances. Finance sets these; the engine applies them."""

CENT = 0.005                 # amounts equal to the cent
FEE_TOLERANCE_ABS = 35.00    # max bank fee absorbed without review (AUD)
FEE_TOLERANCE_PCT = 0.02     # ...and never more than 2% of the invoice
ROUNDING_TOLERANCE = 1.00    # rounding differences accepted either way
NAME_MATCH_CUTOFF = 86       # minimum fuzzy score to identify a payer
PAYMENT_WINDOW_DAYS = 60     # days after due date a payment is still plausible
MAX_COMBINATION_CANDIDATES = 14
MAX_COMBINATION_SIZE = 4


def short_payment_within_policy(invoice_amount: float, paid: float) -> bool:
    diff = round(invoice_amount - paid, 2)
    if abs(diff) <= ROUNDING_TOLERANCE:
        return True
    return 0 < diff <= min(FEE_TOLERANCE_ABS, FEE_TOLERANCE_PCT * invoice_amount)
