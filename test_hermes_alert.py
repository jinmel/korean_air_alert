import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
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
    def __init__(self, login_error=None):
        self.login_error = login_error
        self.initialize_calls = 0
        self.login_calls = 0

    def initialize(self):
        self.initialize_calls += 1

    def login_from_env(self, environ):
        self.login_calls += 1
        if self.login_error:
            raise self.login_error


class HermesAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / 'state' / 'seen.json'

    def run_alert(self, candidates=None, results=None, client=None, environ=None,
                  route=ROUTE):
        client = client or FakeClient()
        candidates = candidates or {}
        results = results or {}
        lookup = lambda _client, origin, destination, *_args, **_kwargs: (
            candidates.get(f'{origin}-{destination}', []), 1)
        verify = lambda _client, origin, destination, rows: results.get(
            f'{origin}-{destination}', [])
        output = io.StringIO()
        with (patch('hermes_alert.find_candidates', side_effect=lookup),
              patch('hermes_alert.verify_candidates', side_effect=verify),
              redirect_stdout(output)):
            result = hermes_alert.run(
                ['--route', route], environ=CREDS if environ is None else environ,
                client_factory=lambda: client, state_path=self.state_path,
                now=datetime(2027, 10, 22, 9, 0, tzinfo=KST))
        return result, output.getvalue(), client

    def test_no_candidates_is_silent_and_does_not_log_in(self):
        result, output, client = self.run_alert()
        self.assertEqual((result, output), (0, ''))
        self.assertEqual(client.login_calls, 0)
        self.assertEqual(json.loads(self.state_path.read_text())['selectable'], {})

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


if __name__ == '__main__':
    unittest.main()
