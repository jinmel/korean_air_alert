import unittest
from datetime import date

from korean_award import change_month, extract, month_start, next_month


class CalendarTests(unittest.TestCase):
    def test_month_range_crosses_year(self):
        self.assertEqual(next_month(month_start('2026-12')), date(2027, 1, 1))

    def test_request_month_end_and_route_preserved(self):
        source = {'departureDate': '2026-10-01', 'endDate': '2026-10-31',
                  'departureAirport': 'ICN', 'arrivalAirport': 'JFK',
                  'unrelated': '2026-10'}
        target = change_month(source, date(2026, 10, 1), date(2027, 2, 1))
        self.assertEqual(target['departureDate'], '2027-02-01')
        self.assertEqual(target['endDate'], '2027-02-28')
        self.assertEqual(target['arrivalAirport'], 'JFK')
        self.assertEqual(target['unrelated'], '2026-10')
        self.assertEqual(source['departureDate'], '2026-10-01')

    def test_unrecognized_request_fails(self):
        with self.assertRaises(ValueError):
            change_month({'year': 2026, 'month': 10}, date(2026, 10, 1), date(2026, 11, 1))

    def test_mixed_month_request_fails(self):
        with self.assertRaises(ValueError):
            change_month({'departureDate': '2026-10-01', 'returnDate': '2026-11-01'},
                         date(2026, 10, 1), date(2026, 11, 1))

    def payload(self, day='2026-10-27'):
        return {'flightList': [{'departureDate': day.replace('-', ''), 'flightDetailList': [
            {'flightNumber': 'KE081', 'bookingClass': code, 'availableSeat': available}
            for code, available in [('O', True), ('A', True), ('Z', True), ('X', True)]]
        }]}

    def test_only_awards_not_upgrades(self):
        rows = extract(self.payload(), date(2026, 10, 27), date(2026, 10, 31))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[3] for r in rows}, {'프레스티지석 보너스', '일등석 보너스'})

    def test_past_dates_excluded(self):
        self.assertEqual(extract(self.payload('2026-10-26'), date(2026, 10, 27), date(2026, 10, 31)), [])

    def test_repeated_month_response_fails(self):
        with self.assertRaises(ValueError):
            extract(self.payload(), date(2026, 11, 1), date(2026, 11, 30))

    def test_empty_or_changed_schema_is_not_no_seats(self):
        for payload in ({'flightList': []}, {'error': 'session expired'},
                        {'bookingClass': 'O', 'availableSeat': 'true', 'flightDate': '2026-10-27'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                extract(payload, date(2026, 10, 27), date(2026, 10, 31))


if __name__ == '__main__':
    unittest.main()
