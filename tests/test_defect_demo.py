
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from defect_demo import calculate_bill_with_defect


def test_defect_demo_detects_incorrect_bill():
    units = 150

    expected_bill = 550
    actual_bill = calculate_bill_with_defect(units)

    assert actual_bill == expected_bill, (
        f"Defect detected! For {units} units, "
        f"expected Rs. {expected_bill}, but got Rs. {actual_bill}."
    )
