import io
import json
import stat
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import hermes_alert
from korean_award import KST


ROW = ('2027-10-22', 'KE081', '10:00', '프레스티지석 보너스')
ROUTE = 'ICN-JFK'
CREDS = {
    'KOREANAIR_USERNAME': 'private-test-user',
    'KOREANAIR_PASSWORD': 'private-test-password',
}


class FakeClient:
    def __init__(self, login_error=None, session_error=None,
                 rotated_token='rotated-session-token'):
        self.login_error = login_error
        self.session_error = session_error
        self.rotated_token = rotated_token
        self.initialize_calls = 0
        self.login_calls = 0
        self.session_calls = []
        self.live_calls = []

    def initialize(self):
        self.initialize_calls += 1

    def login_from_session(self, token):
        self.session_calls.append(token)
        if self.session_error:
            raise self.session_error
        return self.rotated_token

    def login_from_env(self, environ):
        self.login_calls += 1
        if self.login_error:
            raise self.login_error

    def fetch_live(self, origin, destination, day):
        self.live_calls.append(day.isoformat())
        return {'upsellBoundAvailList': [{'availFlightList': []}]}


class SessionClient(FakeClient):
    """Accepts only the given tokens, mapping each to its rotated value."""

    def __init__(self, valid, leak=False):
        super().__init__()
        self.valid = valid
        self.leak = leak

    def login_from_session(self, token):
        self.session_calls.append(token)
        if token not in self.valid:
            raise RuntimeError(f'세션 만료 {token}' if self.leak else '세션 만료')
        return self.valid[token]


class HermesAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / 'state' / 'seen.json'

    def run_alert(self, candidates=None, results=None, client=None, environ=None,
                  route=ROUTE, session_file=None, extra_args=(), now=None,
                  lookup_calls=None, real_verification=False):
        client = client or FakeClient()
        candidates = candidates or {}
        results = results or {}

        def lookup(_client, origin, destination, *_args, **_kwargs):
            if lookup_calls is not None:
                lookup_calls.append(f'{origin}-{destination}')
            return candidates.get(f'{origin}-{destination}', []), 1

        def verify(_client, origin, destination, rows):
            return results.get(f'{origin}-{destination}', [])

        output = io.StringIO()
        argv = ['--route', route, *extra_args]
        if session_file is not None:
            argv.extend(('--session', str(session_file)))
        verification_patch = (
            nullcontext() if real_verification
            else patch('hermes_alert.verify_candidates', side_effect=verify))
        with (patch('hermes_alert.find_candidates', side_effect=lookup),
              verification_patch,
              patch('korean_award.time.sleep'),
              redirect_stdout(output)):
            result = hermes_alert.run(
                argv, environ=CREDS if environ is None else environ,
                client_factory=lambda: client, state_path=self.state_path,
                now=now or datetime(2027, 10, 22, 9, 0, tzinfo=KST))
        return result, output.getvalue(), client

    def test_no_candidates_is_silent_and_does_not_log_in(self):
        result, output, client = self.run_alert()
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.login_calls, 0)
        self.assertEqual(json.loads(self.state_path.read_text())['selectable'], {})
        self.assertEqual(json.loads(self.state_path.read_text())['public'][ROUTE]['rows'], [])

    def test_candidates_that_are_not_selectable_are_silent(self):
        result, output, client = self.run_alert(
            {ROUTE: [ROW]}, {ROUTE: [(ROW, '예매 검색에서 선택 불가', None)]})
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.login_calls, 1)

    def test_new_selectable_alert_and_state_permissions(self):
        result, output, _ = self.run_alert(
            {ROUTE: [ROW]}, {ROUTE: [(ROW, '예매 검색에서 선택 가능', 2)]})
        self.assertEqual(result, 0)
        self.assertIn('ICN→JFK 2027-10-22 KE081 10:00 프레스티지석 보너스', output)
        self.assertIn('응답 좌석 수 2', output)
        self.assertEqual(
            stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['selectable']['ICN-JFK|2027-10-22|KE081|프레스티지석 보너스'],
                         {'first_seen': '2027-10-22T09:00:00+09:00', 'seats': 2})

    def test_second_identical_run_is_deduplicated(self):
        candidates = {ROUTE: [ROW]}
        results = {ROUTE: [(ROW, '예매 검색에서 선택 가능', 2)]}
        self.run_alert(candidates, results)
        result, output, _ = self.run_alert(candidates, results)
        self.assertEqual((result, output), (0, ''))

    def test_reopened_selectable_seats_alert_again(self):
        candidates = {ROUTE: [ROW]}
        selectable = {ROUTE: [(ROW, '예매 검색에서 선택 가능', 2)]}
        sold_out = {ROUTE: [(ROW, '예매 검색에서 선택 불가', None)]}
        self.run_alert(candidates, selectable)
        self.assertEqual(self.run_alert(candidates, sold_out)[1], '')
        result, output, _ = self.run_alert(candidates, selectable)
        self.assertEqual(result, 0)
        self.assertIn('응답 좌석 수 2', output)

    def test_login_error_is_one_line_and_leaves_state_unchanged(self):
        self.state_path.parent.mkdir(parents=True)
        original = '{"selectable":{"old":{"first_seen":"then","seats":1}}}\n'
        self.state_path.write_text(original)
        result, output, _ = self.run_alert(
            {ROUTE: [ROW]}, client=FakeClient(RuntimeError('로그인 실패')),
            environ=CREDS)
        self.assertEqual(result, 1)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertNotIn(CREDS['KOREANAIR_USERNAME'], output)
        self.assertNotIn(CREDS['KOREANAIR_PASSWORD'], output)
        self.assertEqual(self.state_path.read_text(), original)

    def test_missing_credentials_with_candidates_fails(self):
        result, output, client = self.run_alert(
            {ROUTE: [ROW]}, environ={})
        self.assertEqual(result, 1)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn('실패:', output)
        self.assertEqual(client.login_calls, 0)

    def test_unknown_candidate_still_reports_selectable_and_returns_error(self):
        other = ('2027-10-23', 'KE085', '19:40', '일등석 보너스')
        result, output, _ = self.run_alert(
            {ROUTE: [ROW, other]},
            {ROUTE: [(ROW, '예매 검색에서 선택 가능', 2),
                     (other, '미확인: 응답 형식 오류', None)]})
        self.assertEqual(result, 1)
        self.assertIn('ICN→JFK 2027-10-22 KE081', output)
        self.assertIn('미확인 1건: 좌석 없음으로 판단하지 마세요.', output)

    def test_invalid_route_is_argparse_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            hermes_alert.run(
                ['--route', 'icn-JFK'], environ={},
                client_factory=FakeClient, state_path=self.state_path)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('--route', stderr.getvalue())

    def test_earliest_dates_preserves_original_row_order(self):
        rows = [
            ('2027-10-25', 'KE085', '19:40', '일등석 보너스'),
            ('2027-10-21', 'KE081', '10:00', '프레스티지석 보너스'),
            ('2027-10-23', 'KE083', '14:00', '프레스티지석 보너스'),
            ('2027-10-21', 'KE087', '20:00', '일등석 보너스'),
            ('2027-10-24', 'KE089', '22:00', '프레스티지석 보너스'),
            ('2027-10-22', 'KE091', '08:00', '프레스티지석 보너스'),
        ]
        expected = [rows[1], rows[2], rows[3], rows[5]]
        self.assertEqual(hermes_alert._earliest_dates(rows, 3), expected)
        self.assertEqual(hermes_alert._earliest_dates(rows, 0), rows)

    def test_live_dates_limits_verification_but_caches_all_public_rows(self):
        rows = [
            ('2027-10-24', 'KE085', '19:40', '일등석 보너스'),
            ('2027-10-22', 'KE081', '10:00', '프레스티지석 보너스'),
            ('2027-10-21', 'KE083', '14:00', '프레스티지석 보너스'),
            ('2027-10-23', 'KE087', '20:00', '일등석 보너스'),
            ('2027-10-22', 'KE089', '22:00', '프레스티지석 보너스'),
        ]
        client = FakeClient()
        self.run_alert({ROUTE: rows}, client=client, real_verification=True)
        self.assertEqual(len(client.live_calls), 3)
        self.assertEqual(set(client.live_calls), {
            '2027-10-21', '2027-10-22', '2027-10-23',
        })
        cached_rows = json.loads(self.state_path.read_text())['public'][ROUTE]['rows']
        self.assertEqual(cached_rows, [list(row) for row in rows])

        client.live_calls.clear()
        self.run_alert(client=client, extra_args=('--live-dates', '0'),
                       real_verification=True)
        self.assertEqual(len(client.live_calls), 4)
        self.assertEqual(set(client.live_calls), {
            '2027-10-21', '2027-10-22', '2027-10-23', '2027-10-24',
        })

    def test_negative_live_dates_is_argparse_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            hermes_alert.run(
                ['--route', ROUTE, '--live-dates', '-1'], environ={},
                client_factory=FakeClient, state_path=self.state_path)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('--live-dates', stderr.getvalue())

    def test_session_refresh_is_saved_even_without_candidates(self):
        session_file = Path(self.directory.name) / 'session.json'
        session_file.write_text(json.dumps({'T': 'old-session-token'}))
        client = FakeClient()
        result, output, client = self.run_alert(
            client=client, environ={}, session_file=session_file)
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.session_calls, ['old-session-token'])
        self.assertEqual(json.loads(session_file.read_text())['T'], client.rotated_token)
        self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)

    def test_expired_session_leaves_alert_state_unchanged(self):
        self.state_path.parent.mkdir(parents=True)
        original = '{"selectable":{"old":{"first_seen":"then","seats":1}},"public":{}}\n'
        self.state_path.write_text(original)
        session_file = Path(self.directory.name) / 'session.json'
        session_file.write_text(json.dumps({'T': 'expired-private-token'}))
        result, output, _ = self.run_alert(
            client=FakeClient(session_error=RuntimeError(
                '세션 만료 expired-private-token')),
            environ={}, session_file=session_file)
        self.assertEqual(result, 1)
        self.assertIn('대한항공 보너스 알림 실패:', output)
        self.assertNotIn('expired-private-token', output)
        self.assertEqual(self.state_path.read_text(), original)

    def test_environment_token_seeds_missing_session_file(self):
        session_file = Path(self.directory.name) / 'session.json'
        result, output, client = self.run_alert(
            environ={'KOREANAIR_SESSION_T': 'environment-session-token'},
            session_file=session_file)
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.session_calls, ['environment-session-token'])
        self.assertEqual(json.loads(session_file.read_text())['T'],
                         'rotated-session-token')

    def test_save_session_success_and_failure(self):
        session_file = Path(self.directory.name) / 'session.json'
        client = FakeClient()
        output = io.StringIO()
        with (patch('hermes_alert.getpass.getpass',
                    return_value=' typed-session-token \n'),
              redirect_stdout(output)):
            result = hermes_alert.run(
                ['--save-session', '--session', str(session_file)],
                environ={}, client_factory=lambda: client,
                now=datetime(2027, 10, 22, 9, 0, tzinfo=KST))
        self.assertEqual(result, 0)
        self.assertIn('세션 저장 완료 (확인 2027-10-22T09:00:00+09:00)', output.getvalue())
        self.assertEqual(client.session_calls, ['typed-session-token'])
        self.assertEqual(json.loads(session_file.read_text())['T'], client.rotated_token)
        self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)

        failed_file = Path(self.directory.name) / 'failed-session.json'
        failed_client = FakeClient(session_error=RuntimeError(
            '세션 만료 one-time-private-token'))
        output = io.StringIO()
        with (patch('hermes_alert.getpass.getpass',
                    return_value='one-time-private-token'),
              redirect_stdout(output)):
            result = hermes_alert.run(
                ['--save-session', '--session', str(failed_file)],
                environ={}, client_factory=lambda: failed_client,
                now=datetime(2027, 10, 22, 9, 0, tzinfo=KST))
        self.assertEqual(result, 1)
        self.assertIn('대한항공 보너스 알림 실패:', output.getvalue())
        self.assertNotIn('one-time-private-token', output.getvalue())
        self.assertFalse(failed_file.exists())

    def run_with_chrome(self, client, session_token=None, grabbed='chrome-session-token',
                        grab_error=None, push_error=None, extra_args=('--cdp', 'http://cdp')):
        session_file = Path(self.directory.name) / 'session.json'
        if session_token:
            session_file.write_text(json.dumps({'T': session_token}))
        grab_calls, push_calls = [], []

        def grab(url):
            grab_calls.append(url)
            if grab_error:
                raise grab_error
            return grabbed

        def push(token, url):
            push_calls.append((token, url))
            if push_error:
                raise push_error

        with (patch('hermes_alert.grab_session_token', side_effect=grab),
              patch('hermes_alert.push_session_token', side_effect=push)):
            result, output, client = self.run_alert(
                client=client, environ={}, session_file=session_file,
                extra_args=extra_args)
        return result, output, session_file, grab_calls, push_calls

    def test_expired_session_is_replaced_from_chrome(self):
        client = SessionClient({'chrome-session-token': 'rotated-from-chrome'})
        result, output, session_file, grabs, pushes = self.run_with_chrome(
            client, session_token='expired-private-token')
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.session_calls, ['expired-private-token', 'chrome-session-token'])
        self.assertEqual(grabs, ['http://cdp'])
        self.assertEqual(json.loads(session_file.read_text())['T'], 'rotated-from-chrome')
        self.assertEqual(pushes, [('rotated-from-chrome', 'http://cdp')])

    def test_missing_session_is_grabbed_from_chrome(self):
        client = SessionClient({'chrome-session-token': 'rotated-from-chrome'})
        result, _, session_file, grabs, _ = self.run_with_chrome(client)
        self.assertEqual(result, 0)
        self.assertEqual(client.session_calls, ['chrome-session-token'])
        self.assertEqual(grabs, ['http://cdp'])
        self.assertEqual(json.loads(session_file.read_text())['T'], 'rotated-from-chrome')

    def test_valid_session_does_not_grab_but_syncs_chrome(self):
        client = SessionClient({'stored-token': 'rotated-stored'})
        result, _, _, grabs, pushes = self.run_with_chrome(client, session_token='stored-token')
        self.assertEqual(result, 0)
        self.assertEqual(grabs, [])
        self.assertEqual(pushes, [('rotated-stored', 'http://cdp')])

    def test_same_expired_token_in_chrome_is_not_retried(self):
        client = SessionClient({})
        result, output, _, _, pushes = self.run_with_chrome(
            client, session_token='expired-private-token', grabbed='expired-private-token')
        self.assertEqual(result, 1)
        self.assertEqual(client.session_calls, ['expired-private-token'])
        self.assertIn('다시 로그인', output)
        self.assertNotIn('expired-private-token', output)
        self.assertEqual(pushes, [])

    def test_chrome_grab_failure_combines_errors(self):
        client = SessionClient({})
        result, output, _, _, _ = self.run_with_chrome(
            client, session_token='expired-private-token',
            grab_error=RuntimeError('Chrome 원격 디버깅(http://cdp)에 연결할 수 없습니다.'))
        self.assertEqual(result, 1)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn('세션 만료', output)
        self.assertIn('Chrome 세션 가져오기 실패', output)
        self.assertNotIn('expired-private-token', output)

    def test_grabbed_token_failure_is_redacted(self):
        client = SessionClient({}, leak=True)
        result, output, _, _, _ = self.run_with_chrome(client, grabbed='grabbed-private-token')
        self.assertEqual(result, 1)
        self.assertNotIn('grabbed-private-token', output)

    def test_push_failure_does_not_change_result(self):
        client = SessionClient({'stored-token': 'rotated-stored'})
        result, output, _, _, _ = self.run_with_chrome(
            client, session_token='stored-token', push_error=RuntimeError('boom'))
        self.assertEqual((result, output), (0, ''))

    def test_without_cdp_chrome_is_never_used(self):
        client = SessionClient({})
        result, _, _, grabs, pushes = self.run_with_chrome(
            client, session_token='expired-private-token', extra_args=())
        self.assertEqual(result, 1)
        self.assertEqual((grabs, pushes), ([], []))

    def test_grab_session_success_and_failure(self):
        for grabbed, expected in (('chrome-session-token', 0), ('bad-private-token', 1)):
            session_file = Path(self.directory.name) / f'{grabbed}.json'
            client = SessionClient({'chrome-session-token': 'rotated-from-chrome'})
            output = io.StringIO()
            with (patch('hermes_alert.grab_session_token', return_value=grabbed) as grab,
                  redirect_stdout(output)):
                result = hermes_alert.run(
                    ['--grab-session', '--session', str(session_file)],
                    environ={}, client_factory=lambda: client,
                    now=datetime(2027, 10, 22, 9, 0, tzinfo=KST))
            grab.assert_called_once_with('http://127.0.0.1:9222')
            self.assertEqual(result, expected)
            self.assertNotIn(grabbed, output.getvalue())
            if expected == 0:
                self.assertIn('세션 저장 완료 (Chrome에서 가져옴, 확인', output.getvalue())
                self.assertEqual(json.loads(session_file.read_text())['T'], 'rotated-from-chrome')
            else:
                self.assertFalse(session_file.exists())

    def test_public_cache_skips_scan_until_expired_or_disabled(self):
        calls = []
        self.run_alert(lookup_calls=calls)
        self.run_alert(lookup_calls=calls)
        self.assertEqual(calls, [ROUTE])

        later = datetime(2027, 10, 22, 13, 0, tzinfo=KST)
        self.run_alert(lookup_calls=calls, now=later)
        self.assertEqual(calls, [ROUTE, ROUTE])

        self.run_alert(lookup_calls=calls, now=later,
                       extra_args=('--public-cache-hours', '0'))
        self.assertEqual(calls, [ROUTE, ROUTE, ROUTE])

    def test_public_cache_rescans_when_window_start_changes(self):
        calls = []
        hours = ('--public-cache-hours', '48')
        self.run_alert(lookup_calls=calls, extra_args=hours)
        next_day = datetime(2027, 10, 23, 9, 0, tzinfo=KST)
        self.run_alert(lookup_calls=calls, extra_args=hours, now=next_day)
        self.assertEqual(calls, [ROUTE, ROUTE])

    def test_old_state_without_public_cache_is_supported(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text('{"selectable":{}}\n')
        result, output, _ = self.run_alert()
        self.assertEqual((result, output), (0, ''))
        self.assertIn('public', json.loads(self.state_path.read_text()))


if __name__ == '__main__':
    unittest.main()
