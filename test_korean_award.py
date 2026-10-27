import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date

from korean_award import AwardClient, extract, month_start, next_month


class CalendarTests(unittest.TestCase):
    def test_month_range_crosses_year(self):
        self.assertEqual(next_month(month_start('2026-12')), date(2027, 1, 1))

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


class HTTPTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Set-Cookie', 'award-test=1; Path=/; HttpOnly')
                self.end_headers()
                self.wfile.write(b'<html>Calendar</html>')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.last_body = body
                if self.headers.get('Cookie') != 'award-test=1' or self.server.mode == 'denied':
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'Access Denied')
                    return
                self.send_response(200)
                self.end_headers()
                if self.server.mode == 'html':
                    self.wfile.write(b'<html>Login required</html>')
                    return
                payload = {
                    'departureAirport': body['departureAirport'],
                    'arrivalAirport': 'LAX' if self.server.mode == 'wrong-route' else body['arrivalAirport'],
                    'flightList': [],
                }
                self.wfile.write(json.dumps(payload).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.mode = 'ok'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AwardClient(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_normal_http_session_retains_cookie_and_changes_month(self):
        self.client.initialize()
        self.client.fetch_month('ICN', 'JFK', date(2026, 12, 1))
        self.client.fetch_month('ICN', 'JFK', date(2027, 1, 1))
        self.assertEqual(self.server.last_body, {
            'departureAirport': 'ICN', 'arrivalAirport': 'JFK', 'departureDate': '20270101'})

    def test_missing_session_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.client.fetch_month('ICN', 'JFK', date(2027, 1, 1))

    def test_denial_is_reported_even_with_session(self):
        self.client.initialize()
        self.server.mode = 'denied'
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.client.fetch_month('ICN', 'JFK', date(2027, 1, 1))

    def test_html_is_not_availability(self):
        self.client.initialize()
        self.server.mode = 'html'
        with self.assertRaisesRegex(ValueError, 'JSON'):
            self.client.fetch_month('ICN', 'JFK', date(2027, 1, 1))

    def test_wrong_route_is_rejected(self):
        self.client.initialize()
        self.server.mode = 'wrong-route'
        with self.assertRaisesRegex(ValueError, '노선'):
            self.client.fetch_month('ICN', 'JFK', date(2027, 1, 1))


if __name__ == '__main__':
    unittest.main()
