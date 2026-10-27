import unittest
import json
import threading
import io
from contextlib import redirect_stdout
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, timedelta
from unittest.mock import patch

from korean_award import (
    AwardClient, LOGIN_PATH, LIVE_PATH, extract, find_candidates, live_result,
    login_settings, month_start, next_month, query_window,
)


class HelperTests(unittest.TestCase):
    def test_query_window_validation_messages(self):
        today = date(2027, 10, 22)
        with self.assertRaisesRegex(ValueError, '일수는 1 이상이어야 합니다.'):
            query_window(today, days=0)
        with self.assertRaisesRegex(ValueError, '시작 월이 조회 기간 밖입니다.'):
            query_window(today, month='2027-09')

    def test_find_candidates_without_output_is_silent(self):
        class Client:
            def fetch_month(self, origin, destination, month):
                return {'flightList': [{'departureDate': '20271022',
                                        'flightDetailList': [{
                                            'bookingClass': 'O',
                                            'availableSeat': False,
                                        }]}]}

        output = io.StringIO()
        with patch('korean_award.time.sleep'), redirect_stdout(output):
            rows, completed = find_candidates(
                Client(), 'ICN', 'JFK', date(2027, 10, 1),
                date(2027, 10, 22), date(2027, 10, 22))
        self.assertEqual(rows, [])
        self.assertEqual(completed, 1)
        self.assertEqual(output.getvalue(), '')


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


LIVE_ROW = ('2027-10-22', 'KE081', '10:00', '프레스티지석 보너스')


def live_fixture(count='2'):
    # Shape and selection fields taken from the official site's public JS,
    # not a captured authenticated production response.
    return {'upsellBoundAvailList': [{'availFlightList': [{
        'flightInfoList': [{'carrierCode': 'KE', 'flightNumber': '81',
                           'departureAirport': 'ICN', 'arrivalAirport': 'JFK',
                           'departureDateTime': '202710221000'}],
        'commercialFareFamilyList': [{'fareFamily': 'KEBONUSPR', 'bookingClass': 'O',
                                     'seatCount': count, 'recommendId': 'test-offer',
                                     'soldout': False}],
    }]}]}


class LiveResultTests(unittest.TestCase):
    def check(self, payload):
        return live_result(payload, 'ICN', 'JFK', LIVE_ROW)

    def test_positive_matching_award_is_selectable(self):
        self.assertEqual(self.check(live_fixture()), ('예매 검색에서 선택 가능', 2))

    def test_zero_or_disabled_not_selectable(self):
        for count in ('0', ''):
            with self.subTest(count=count):
                self.assertEqual(self.check(live_fixture(count))[0], '예매 검색에서 선택 불가')

    def test_soldout_overrides_positive_count(self):
        payload = live_fixture()
        payload['upsellBoundAvailList'][0]['availFlightList'][0]['commercialFareFamilyList'][0]['soldout'] = True
        self.assertEqual(self.check(payload)[0], '예매 검색에서 선택 불가')

    def test_production_soldout_shape_with_empty_booking_class(self):
        payload = live_fixture('0')
        segment = payload['upsellBoundAvailList'][0]['availFlightList'][0]
        segment['flightInfoList'][0]['departureDateTime'] = '20271022100000'
        fare = segment['commercialFareFamilyList'][0]
        fare['bookingClass'] = ''
        fare['soldout'] = True
        self.assertEqual(self.check(payload), ('예매 검색에서 선택 불가', None))

    def test_unknown_seat_count_is_not_confirmation(self):
        for count in (None, True, 'unknown', '9+', -1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.check(live_fixture(count))

    def test_wrong_date_or_route_rejected(self):
        for field, value in (('departureDateTime', '202710231000'), ('arrivalAirport', 'LAX')):
            payload = live_fixture()
            payload['upsellBoundAvailList'][0]['availFlightList'][0]['flightInfoList'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(payload)

    def test_login_error_or_changed_schema_never_means_no_seats(self):
        for payload in ({'errors': ['expired']}, {'code': 'AUTH'}, {'data': []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.check(payload)

    def test_missing_flight_or_fare_is_unknown(self):
        payload = live_fixture()
        payload['upsellBoundAvailList'][0]['availFlightList'] = []
        self.assertTrue(self.check(payload)[0].startswith('미확인'))
        payload = live_fixture()
        payload['upsellBoundAvailList'][0]['availFlightList'][0]['commercialFareFamilyList'][0]['fareFamily'] = 'KEBONUSEY'
        self.assertTrue(self.check(payload)[0].startswith('미확인'))

    def test_cash_or_upgrade_class_never_confirmed_as_award(self):
        payload = live_fixture()
        payload['upsellBoundAvailList'][0]['availFlightList'][0]['commercialFareFamilyList'][0]['bookingClass'] = 'Z'
        with self.assertRaises(ValueError):
            self.check(payload)

    def test_missing_offer_id_is_not_selectable(self):
        payload = live_fixture()
        payload['upsellBoundAvailList'][0]['availFlightList'][0]['commercialFareFamilyList'][0]['recommendId'] = ''
        with self.assertRaises(ValueError):
            self.check(payload)


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
                if self.path == LOGIN_PATH:
                    self.server.login_calls += 1
                    self.server.login_body = deepcopy(body)
                    self.server.login_session_id = self.headers.get('ksessionId')
                    self.server.login_timestamp = self.headers.get('timestamp')
                    if self.server.mode == 'login-http403':
                        self.send_response(403)
                        self.end_headers()
                        self.wfile.write(json.dumps({'message': body['password']}).encode())
                        return
                    self.send_response(200)
                    if self.server.mode != 'login-fail':
                        self.send_header('Set-Cookie', 'auth-test=1; Path=/; HttpOnly')
                    self.end_headers()
                    if self.server.mode == 'login-fail':
                        self.wfile.write(json.dumps({'code': 'AUTH_FAIL', 'message': body['password']}).encode())
                    else:
                        self.wfile.write(json.dumps({'userInfo': {
                            'skypassNumber': 'TEST-ACCOUNT', 'englishFirstName': 'TEST',
                            'englishLastName': 'USER'}}).encode())
                    return
                if self.path == LIVE_PATH:
                    if 'auth-test=1' not in self.headers.get('Cookie', ''):
                        self.send_response(401)
                        self.end_headers()
                        return
                    self.server.live_calls += 1
                    self.server.live_session_id = self.headers.get('ksessionId')
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(live_fixture()).encode())
                    return
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
        self.server.login_calls = 0
        self.server.live_calls = 0
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


    def test_env_credentials_login_and_read_only_search(self):
        self.client.initialize()
        self.client.login_from_env({'KOREANAIR_USERNAME': 'test-id', 'KOREANAIR_PASSWORD': 'test-password'})
        self.assertEqual(self.server.login_body, {'userId': 'test-id', 'password': 'test-password'})
        result = self.client.fetch_live('ICN', 'JFK', date(2027, 10, 22))
        self.assertEqual(live_result(result, 'ICN', 'JFK', LIVE_ROW)[0], '예매 검색에서 선택 가능')
        self.assertEqual(self.server.live_calls, 1)
        self.assertRegex(self.server.login_session_id, r'^[a-f0-9]{32}\d{8}W$')
        self.assertEqual(self.server.live_session_id, self.server.login_session_id)
        self.assertRegex(self.server.login_timestamp, r'^\d{13}$')
        self.assertEqual(self.server.last_body['segmentList'][0]['departureDate'], '20271022')
        self.assertEqual(self.server.last_body['commercialFareFamilies'], ['KEBONUSNEW'])
        self.assertEqual(len(self.server.last_body['travelers']), 1)
        self.assertNotIn('password', self.server.last_body)

    def test_skypass_login_type(self):
        self.client.initialize()
        self.client.login_from_env({'KOREANAIR_USERNAME': 'test-skypass',
                                    'KOREANAIR_PASSWORD': 'test-password', 'KOREANAIR_LOGIN_TYPE': 'skypass'})
        self.assertEqual(self.server.login_body['skypassNumber'], 'test-skypass')
        self.assertNotIn('userId', self.server.login_body)

    def test_missing_credentials_do_not_attempt_login(self):
        with self.assertRaisesRegex(ValueError, '환경 변수'):
            self.client.login_from_env({})
        self.assertEqual(self.server.login_calls, 0)

    def test_login_failure_does_not_echo_password_or_retry(self):
        self.client.initialize()
        self.server.mode = 'login-fail'
        with self.assertRaises(RuntimeError) as caught:
            self.client.login_from_env({'KOREANAIR_USERNAME': 'private-id', 'KOREANAIR_PASSWORD': 'private-password'})
        self.assertNotIn('private-password', str(caught.exception))
        self.assertNotIn('private-id', str(caught.exception))
        self.assertEqual(self.server.login_calls, 1)
        self.assertIsNone(self.client.traveler)

    def test_login_http_status_retained_without_secret_response(self):
        self.client.initialize()
        self.server.mode = 'login-http403'
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as caught:
            self.client.login_from_env({'KOREANAIR_USERNAME': 'private-id', 'KOREANAIR_PASSWORD': 'private-password'})
        self.assertNotIn('private-password', str(caught.exception))
        self.assertNotIn('private-id', str(caught.exception))
        self.assertEqual(self.server.login_calls, 1)

    def test_live_search_requires_login(self):
        with self.assertRaisesRegex(RuntimeError, '로그인'):
            self.client.fetch_live('ICN', 'JFK', date(2027, 10, 22))
        self.assertEqual(self.server.live_calls, 0)


if __name__ == '__main__':
    unittest.main()
