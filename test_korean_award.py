import unittest
import json
import stat
import tempfile
import threading
import io
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from korean_award import (
    AwardClient, KST, LIVE_PATH, LOGIN_PATH, REFRESH_PATH, USER_INFO_PATH,
    UPGRADE_CABINS, date_window, extract, find_candidates, live_result,
    login_settings, main, month_start, next_month, parse_routes, query_window,
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

    def test_date_window_defaults_rejects_and_clips(self):
        today = date(2027, 10, 22)
        self.assertEqual(date_window(today), (
            date(2027, 10, 1), today, today + timedelta(days=360)))
        self.assertEqual(date_window(today, '2027-11-02', '2028-12-31', 30), (
            date(2027, 11, 1), date(2027, 11, 2), today + timedelta(days=30)))
        with self.assertRaisesRegex(ValueError, '시작일이 오늘'):
            date_window(today, '2027-10-21')
        with self.assertRaisesRegex(ValueError, '종료일이 시작일'):
            date_window(today, '2027-10-23', '2027-10-22')
        with self.assertRaisesRegex(ValueError, '시작일이 조회 한도'):
            date_window(today, '2028-10-18', days=360)

    def test_parse_routes_normalizes_deduplicates_and_rejects_invalid_routes(self):
        self.assertEqual(parse_routes(' icn-jfk, ICN-FRA,icn-jfk '),
                         [('ICN', 'JFK'), ('ICN', 'FRA')])
        for value in ('ICN-JFK, bad', 'ICN-ICN', ','):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_routes(value)


class CliClient:
    def __init__(self, fail_public_route=None, live_unknown=False, live_error=False):
        self.fail_public_route = fail_public_route
        self.live_unknown = live_unknown
        self.live_error = live_error
        self.session_calls = []
        self.env_login_calls = 0

    def initialize(self):
        pass

    def fetch_month(self, origin, destination, month):
        if f'{origin}-{destination}' == self.fail_public_route:
            raise RuntimeError('공개 조회 실패')
        return {'flightList': [{
            'departureDate': '20271022',
            'flightNumber': 'KE081',
            'flightDetailList': [
                {'bookingClass': code, 'availableSeat': True,
                 'departureTime': '10:00'}
                for code in ('O', 'A', 'Z')
            ],
        }]}

    def login_from_session(self, token):
        self.session_calls.append(token)
        return 'rotated-cli-session-token'

    def login_from_env(self, environ=None):
        self.env_login_calls += 1

    def fetch_live(self, origin, destination, day):
        if self.live_error:
            raise RuntimeError('실시간 검색 실패')
        if self.live_unknown:
            return {'errors': ['unknown']}
        fares = [
            {'fareFamily': 'KEBONUSPR', 'bookingClass': 'O',
             'seatCount': '2', 'recommendId': 'offer-o', 'soldout': False},
            {'fareFamily': 'KEBONUSFC', 'bookingClass': 'A',
             'seatCount': '1', 'recommendId': 'offer-a', 'soldout': False},
        ]
        return {'upsellBoundAvailList': [{'availFlightList': [{
            'flightInfoList': [{
                'carrierCode': 'KE', 'flightNumber': '81',
                'departureAirport': origin, 'arrivalAirport': destination,
                'departureDateTime': day.strftime('%Y%m%d') + '1000',
            }],
            'commercialFareFamilyList': fares,
        }]}]}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2027, 10, 22, 9, 0, tzinfo=KST)

    def run_cli(self, argv, client=None, environ=None):
        output = io.StringIO()
        client = client or CliClient()
        with (patch('korean_award.time.sleep'), redirect_stdout(output)):
            result = main(
                argv, environ=({
                    'KOREANAIR_USERNAME': 'cli-test-user',
                    'KOREANAIR_PASSWORD': 'cli-test-password',
                } if environ is None else environ), client_factory=lambda: client,
                now=self.now)
        return result, output.getvalue(), client

    def json_args(self, *extra):
        return [
            '--routes', 'ICN-JFK', '--start', '2027-10-22',
            '--end', '2027-10-22', '--json', *extra,
        ]

    def test_routes_options_validate_and_legacy_route_works(self):
        for argv in (
                ['--routes', 'ICN-JFK', '--origin', 'ICN', '--destination', 'JFK'],
                ['--origin', 'ICN'],
                ['--routes', 'ICN-ICN']):
            stderr = io.StringIO()
            with self.subTest(argv=argv), redirect_stderr(stderr), self.assertRaises(SystemExit):
                main(argv, environ={}, now=self.now)
        result, output, _ = self.run_cli([
            '--origin', 'icn', '--destination', 'jfk',
            '--start', '2027-10-22', '--end', '2027-10-22', '--json',
        ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)['routes'][0]['route'], 'ICN-JFK')
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(['--routes', 'ICN-JFK', '--start', '2027-10-22',
                  '--month', '2027-10'], environ={}, now=self.now)

    def test_seat_types_default_upgrade_only_and_both(self):
        result, output, _ = self.run_cli(self.json_args())
        self.assertEqual(result, 0)
        payload = json.loads(output)
        self.assertEqual(payload['seat_types'], ['award'])
        self.assertEqual({row['seat_type'] for row in payload['routes'][0]['results']},
                         {'award'})
        result, output, _ = self.run_cli(self.json_args('--seat-types', 'upgrade'))
        self.assertEqual(result, 0)
        payload = json.loads(output)
        self.assertEqual(payload['routes'][0]['results'][0]['cabin'], '마일리지 승급 좌석')
        result, output, _ = self.run_cli(self.json_args('--seat-types', 'award,upgrade'))
        self.assertEqual(result, 0)
        self.assertEqual(len(json.loads(output)['routes'][0]['results']), 3)

    def test_json_public_only_marks_candidates_not_checked(self):
        result, output, _ = self.run_cli(self.json_args())
        payload = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual(payload['routes'][0]['status'], 'ok')
        self.assertTrue(all(row['live_status'] == 'not_checked'
                            for row in payload['routes'][0]['results']))

    def test_json_live_verification_marks_awards_and_upgrade(self):
        result, output, _ = self.run_cli(
            self.json_args('--verify-live', '--seat-types', 'award,upgrade'))
        payload = json.loads(output)
        self.assertEqual(result, 0)
        statuses = {row['seat_type']: row['live_status']
                    for row in payload['routes'][0]['results']}
        self.assertEqual(statuses, {'award': 'selectable', 'upgrade': 'unsupported'})
        self.assertEqual(payload['summary']['selectable'], 2)
        self.assertEqual(payload['summary']['unsupported'], 1)

    def test_json_unknown_returns_error_code(self):
        result, output, _ = self.run_cli(
            self.json_args('--verify-live'), CliClient(live_unknown=True))
        payload = json.loads(output)
        self.assertEqual(result, 1)
        self.assertTrue(all(row['live_status'] == 'unknown'
                            for row in payload['routes'][0]['results']))

    def test_public_failure_does_not_prevent_other_route(self):
        result, output, _ = self.run_cli(
            ['--routes', 'ICN-JFK,ICN-FRA', '--start', '2027-10-22',
             '--end', '2027-10-22', '--json'],
            CliClient(fail_public_route='ICN-JFK'))
        payload = json.loads(output)
        self.assertEqual(result, 1)
        self.assertEqual([route['status'] for route in payload['routes']],
                         ['error', 'ok'])
        self.assertEqual(payload['routes'][1]['months_completed'], 1)

    def test_live_failure_marks_remaining_routes_and_keeps_rows_unknown(self):
        result, output, _ = self.run_cli(
            ['--routes', 'ICN-JFK,ICN-FRA', '--start', '2027-10-22',
             '--end', '2027-10-22', '--verify-live', '--json'],
            CliClient(live_error=True))
        payload = json.loads(output)
        self.assertEqual(result, 1)
        self.assertEqual([route['status'] for route in payload['routes']],
                         ['error', 'error'])
        self.assertEqual(payload['routes'][1]['error'], '이전 실시간 검색 실패로 중단')
        self.assertTrue(all(row['live_status'] == 'unknown'
                            for row in payload['routes'][1]['results']))

    def test_session_token_is_rotated_and_saved_without_stdout_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            session_path = Path(directory) / 'session.json'
            session_path.write_text(json.dumps({'T': 'private-session-token'}))
            client = CliClient()
            result, output, client = self.run_cli(
                self.json_args('--verify-live', '--session', str(session_path)),
                client=client)
            self.assertEqual(json.loads(output)['routes'][0]['status'], 'ok')
            self.assertEqual(result, 0)
            self.assertEqual(client.session_calls, ['private-session-token'])
            self.assertEqual(json.loads(session_path.read_text())['T'],
                             'rotated-cli-session-token')
            self.assertEqual(stat.S_IMODE(session_path.stat().st_mode), 0o600)
            self.assertNotIn('private-session-token', output)
            self.assertNotIn('rotated-cli-session-token', output)

    def test_expired_session_is_regrabbed_from_chrome_cdp(self):
        class ExpiringClient(CliClient):
            def login_from_session(self, token):
                self.session_calls.append(token)
                if token == 'expired-session-token':
                    raise RuntimeError('세션 만료')
                return 'rotated-cli-session-token'

        with tempfile.TemporaryDirectory() as directory:
            session_path = Path(directory) / 'session.json'
            session_path.write_text(json.dumps({'T': 'expired-session-token'}))
            with patch('hermes_alert.grab_session_token',
                       return_value='chrome-session-token') as grab:
                result, output, client = self.run_cli(
                    self.json_args('--verify-live', '--session', str(session_path),
                                   '--cdp', 'http://127.0.0.1:9222'),
                    client=ExpiringClient())
            grab.assert_called_once_with('http://127.0.0.1:9222')
            self.assertEqual(result, 0)
            self.assertEqual(client.session_calls,
                             ['expired-session-token', 'chrome-session-token'])
            self.assertEqual(json.loads(session_path.read_text())['T'],
                             'rotated-cli-session-token')
            for secret in ('expired-session-token', 'chrome-session-token',
                           'rotated-cli-session-token'):
                self.assertNotIn(secret, output)

    def test_invalid_seat_types_are_argparse_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(self.json_args('--seat-types', 'award,cash'),
                 environ={}, now=self.now)
        self.assertIn('--seat-types', stderr.getvalue())


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
                if self.path == REFRESH_PATH:
                    self.server.refresh_calls += 1
                    if self.server.session_mode == 'refresh-http403':
                        self.send_response(403)
                        self.end_headers()
                        self.wfile.write(b'private response')
                        return
                    self.send_response(200)
                    self.send_header(
                        'Set-Cookie',
                        f'T={self.server.rotated_token}; Domain={self.server.cookie_domain}; '
                        'Path=/; HttpOnly')
                    self.end_headers()
                    return
                if self.path == USER_INFO_PATH:
                    self.server.userinfo_calls += 1
                    authenticated = f'T={self.server.rotated_token}' in self.headers.get('Cookie', '')
                    self.send_response(200)
                    self.end_headers()
                    if self.server.session_mode == 'bad-user-json':
                        self.wfile.write(b'not json')
                    elif not authenticated or self.server.session_mode == 'empty-user-info':
                        self.wfile.write(json.dumps({
                            'skypassNumber': '', 'englishLastName': '',
                            'englishFirstName': '',
                        }).encode())
                    else:
                        self.wfile.write(json.dumps({
                            'skypassNumber': 'SESSION-ACCOUNT',
                            'englishFirstName': 'SESSION',
                            'englishLastName': 'USER',
                        }).encode())
                    return
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
                    cookies = self.headers.get('Cookie', '')
                    if ('auth-test=1' not in cookies
                            and f'T={self.server.rotated_token}' not in cookies):
                        self.send_response(401)
                        self.end_headers()
                        return
                    self.server.live_calls += 1
                    self.server.live_session_id = self.headers.get('ksessionId')
                    self.server.live_body = body
                    self.server.live_cookie = cookies
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
        self.server.session_mode = 'ok'
        self.server.cookie_domain = '127.0.0.1'
        self.server.rotated_token = 'rotated-session-token'
        self.server.login_calls = 0
        self.server.live_calls = 0
        self.server.refresh_calls = 0
        self.server.userinfo_calls = 0
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

    def test_session_login_refreshes_cookie_and_searches_as_traveler(self):
        self.client.initialize()
        rotated = self.client.login_from_session('initial-session-token')
        self.assertEqual(rotated, self.server.rotated_token)
        self.assertEqual(self.server.refresh_calls, 1)
        self.assertEqual(self.server.userinfo_calls, 1)
        self.assertEqual(self.client.traveler, {
            'travellerType': 'ADT', 'fqtvNumber': 'SESSION-ACCOUNT',
            'lastName': 'USER', 'firstName': 'SESSION',
        })
        payload = self.client.fetch_live('ICN', 'JFK', date(2027, 10, 22))
        self.assertEqual(live_result(payload, 'ICN', 'JFK', LIVE_ROW)[0], '예매 검색에서 선택 가능')
        self.assertIn(f'T={self.server.rotated_token}', self.server.live_cookie)
        self.assertEqual(self.server.live_body['travelers'], [self.client.traveler])

    def test_empty_session_user_info_is_sanitized(self):
        self.client.initialize()
        self.server.session_mode = 'empty-user-info'
        with self.assertRaisesRegex(RuntimeError, '세션 만료 또는 로그인 상태가 아닙니다') as caught:
            self.client.login_from_session('private-session-token')
        self.assertNotIn('private-session-token', str(caught.exception))
        self.assertIsNone(self.client.traveler)

    def test_non_json_session_user_info_is_sanitized(self):
        self.client.initialize()
        self.server.session_mode = 'bad-user-json'
        with self.assertRaisesRegex(RuntimeError, '세션 만료 또는 로그인 상태가 아닙니다'):
            self.client.login_from_session('private-session-token')
        self.assertIsNone(self.client.traveler)

    def test_session_refresh_http_status_is_retained_without_response(self):
        self.client.initialize()
        self.server.session_mode = 'refresh-http403'
        with self.assertRaisesRegex(RuntimeError, r'세션 만료.*HTTP 403') as caught:
            self.client.login_from_session('private-session-token')
        self.assertNotIn('private-session-token', str(caught.exception))
        self.assertNotIn('private response', str(caught.exception))
        self.assertIsNone(self.client.traveler)


if __name__ == '__main__':
    unittest.main()
