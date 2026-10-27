
def calculate_bill_with_defect(units):
    """Intentionally incorrect billing function for QA demonstration."""

    if units <= 100:
        return units * 3

    elif units <= 200:
        # INTENTIONAL DEFECT:
        # The first 100 units should cost ₹300,
        # but this function incorrectly charges ₹3 for all units.
        return units * 5

    elif units <= 300:
        return 300 + 100 * 5 + (units - 200) * 7

    else:
        return 300 + 100 * 5 + 100 * 7 + (units - 300) * 8
