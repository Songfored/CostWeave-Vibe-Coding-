import unittest

from costweave.receipt_image_reader import _visual_lines
from costweave.receipt_totals import select_total


def word(text, left, top):
    return {"text": text, "left": left, "right": left + .08, "top": top, "bottom": top + .025}


def view(*lines):
    rows = []
    for index, line in enumerate(lines):
        tokens = line.split()
        rows.append([word(token, .82 - (len(tokens) - n - 1) * .10, .15 + index * .04)
                     for n, token in enumerate(tokens)])
    return rows


class ReceiptTotalTests(unittest.TestCase):
    def test_split_printed_amount_preserves_magnitude_and_decimal_places(self):
        for printed, expected in (("283 , 338", 28333800), ("26 ,000", 2600000),
                                  ("60. 000", 6000000), ("Rp 1.234,50", 123450)):
            with self.subTest(printed=printed):
                self.assertEqual(expected, select_total([view("GRAND TOTAL " + printed)], "check"))

    def test_cash_given_and_change_may_share_one_line(self):
        receipt = view("TUNAI 100,000 KEMBALI 12,000")
        self.assertEqual(8800000, select_total([receipt], "check"))
        self.assertIsNone(select_total([view("CASH 100,000")], "check"))

    def test_signed_charges_follow_the_most_recent_subtotal(self):
        receipt = view("SUBTOTAL 40,000", "SUBTOTAL 80,000", "Service 5% 4,000",
                       "Pajak 10% 8,000", "Diskon 2,000")
        self.assertEqual(9000000, select_total([receipt], "check"))

    def test_damaged_total_can_be_recovered_from_aligned_payment_rows(self):
        receipt = view("Subtotal 60,000", "66,000", "66,000")
        self.assertEqual(6600000, select_total([receipt], "check"))

    def test_incomplete_numbers_and_nonmonetary_labels_do_not_supply_a_total(self):
        for line in ("TOTAL 22,0009", "TOTAL 20%", "TOTAL 24/11/2026",
                     "TOTAL ITEM 300", "Invoice No 135000"):
            with self.subTest(line=line):
                self.assertIsNone(select_total([view(line)], "direct"))

    def test_strict_read_abstains_on_disagreement_or_a_single_unsupported_view(self):
        first, second = view("TOTAL 20,000"), view("TOTAL 30,000")
        self.assertIsNone(select_total([first], "original"))
        self.assertIsNone(select_total([first, second], "original"))
        self.assertEqual(2000000, select_total([first, first], "original"))

    def test_word_geometry_reconstructs_two_rows_in_reading_order(self):
        boxes = [word("35,000", .80, .102), word("TOTAL", .12, .1),
                 word("0", .81, .172), word("CHANGE", .13, .17)]
        rows = _visual_lines(boxes)
        self.assertEqual([["TOTAL", "35,000"], ["CHANGE", "0"]],
                         [[w["text"] for w in row] for row in rows])
        self.assertEqual(3500000, select_total([rows], "check"))

    def test_label_on_its_own_row_requires_a_nearby_amount(self):
        close = [[word("TOTAL", .10, .1)], [word("44,000", .8, .15)]]
        far = [[word("TOTAL", .10, .1)], [word("44,000", .8, .5)]]
        self.assertEqual(4400000, select_total([close], "check"))
        self.assertIsNone(select_total([far], "check"))
