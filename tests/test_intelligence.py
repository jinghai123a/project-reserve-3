import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

from cloudkit.integrations import intelligence as api


class Response(io.BytesIO):
    status = 200

    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class IntelligenceTests(unittest.TestCase):
    key = 'fixture-key+/=private'

    def env(self, provider):
        return {api.KEY_NAMES[provider]: self.key}

    def test_vt_hash_uses_header_auth_and_never_uploads(self):
        opener = Mock(return_value=Response({'data': {'id': 'a' * 64, 'type': 'file'}}))
        result, code = api.lookup('vt', 'file', 'A' * 64, env=self.env('vt'), opener=opener)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://www.virustotal.com/api/v3/files/' + 'a' * 64)
        self.assertEqual(request.get_header('X-apikey'), self.key)
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(opener.call_args.kwargs['timeout'], 15)
        self.assertEqual((result['status'], code), ('DONE', 0))
        self.assertNotIn(self.key, json.dumps(result))

    def test_vt_domain_and_ip_require_explicit_public_submission(self):
        for resource, value in [('domain', 'example.com'), ('ip', '8.8.8.8')]:
            with self.subTest(resource=resource):
                opener = Mock(return_value=Response({'data': {'id': value, 'type': 'domain' if resource == 'domain' else 'ip_address'}}))
                result, code = api.lookup('vt', resource, value, env=self.env('vt'), opener=opener)
                self.assertEqual((result['status'], code), ('BLOCKED_CAPABILITY', 78))
                opener.assert_not_called()
                result, code = api.lookup('vt', resource, value, env=self.env('vt'), opener=opener,
                                          allow_public_submission=True)
                self.assertEqual(code, 0)
                path = '/domains/' if resource == 'domain' else '/ip_addresses/'
                self.assertIn(path + value, opener.call_args.args[0].full_url)

    def test_shodan_host_uses_query_key(self):
        opener = Mock(return_value=Response({'ip_str': '8.8.8.8', 'ports': [53]}))
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener)
        request = opener.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, '/shodan/host/8.8.8.8')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'key': [self.key]})
        self.assertEqual(code, 0)
        self.assertNotIn('?', result['endpoint'])

    def test_maps_encodes_address_and_key(self):
        opener = Mock(return_value=Response({'status': 'OK', 'results': [{'place_id': 'fixture', 'geometry': {'location': {'lat': 37.4, 'lng': -122.1}}}]}))
        address = 'CWC8+R9 Mountain View, CA'
        result, code = api.lookup('maps', 'geocode', address, env=self.env('maps'), opener=opener)
        request = opener.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).netloc, 'maps.googleapis.com')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'address': [address], 'key': [self.key]})
        self.assertEqual((result['status'], code), ('DONE', 0))

    def test_secret_file_source_and_invalid_source_do_not_leak_path_or_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key.txt'
            path.write_text(self.key + '\n')
            env = {'SHODAN_API_KEY_FILE': str(path)}
            opener = Mock(return_value=Response({'ip_str': '8.8.8.8'}))
            result, code = api.lookup('shodan', 'host', '8.8.8.8', env=env, opener=opener)
            self.assertEqual(code, 0)
            self.assertNotIn(str(path), json.dumps(result))
            env['SHODAN_API_KEY'] = self.key
            opener.reset_mock()
            result, code = api.lookup('shodan', 'host', '8.8.8.8', env=env, opener=opener)
            self.assertEqual(code, 78)
            opener.assert_not_called()
        env = {'SHODAN_API_KEY_FILE': '/missing/' + self.key}
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=env, opener=opener)
        self.assertEqual(code, 78)
        self.assertNotIn(self.key, json.dumps(result))

    def test_missing_key_has_no_network_side_effects(self):
        for provider, resource, value in [('vt', 'file', 'a'*64), ('shodan', 'host', '8.8.8.8'), ('maps', 'geocode', 'Washington')]:
            opener = Mock()
            result, code = api.lookup(provider, resource, value, env={}, opener=opener)
            self.assertEqual((result['status'], code), ('BLOCKED_CAPABILITY', 78))
            self.assertEqual(result['attempts'], 0)
            self.assertFalse(result['live_response_received'])
            opener.assert_not_called()

    def test_invalid_indicators_are_rejected_before_network(self):
        cases = [('vt', 'file', '/tmp/file'), ('vt', 'file', 'a'*40),
                 ('vt', 'domain', 'https://example.com'), ('vt', 'domain', 'example.com/x'),
                 ('vt', 'domain', '-bad.example.com'), ('vt', 'domain', 'thing.local'),
                 ('vt', 'domain', '8.8.8.8'), ('vt', 'ip', '10.0.0.1'),
                 ('shodan', 'host', '127.0.0.1'), ('shodan', 'host', 'example.com'),
                 ('maps', 'geocode', 'address\nextra'), ('maps', 'geocode', ''),
                 ('vt', 'scan', 'example.com')]
        for provider, resource, value in cases:
            with self.subTest(case=(provider, resource, value)):
                opener = Mock()
                result, code = api.lookup(provider, resource, value, env=self.env(provider), opener=opener,
                                          allow_public_submission=True)
                self.assertEqual((result['status'], code), ('INVALID_INPUT', 64))
                opener.assert_not_called()

    def test_response_echoed_key_is_redacted_recursively(self):
        payload = {'ip_str': '8.8.8.8', self.key: [self.key, quote(self.key, safe=''), quote_plus(self.key),
                    'https://api.example?key=' + quote_plus(self.key)]}
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'),
                                  opener=Mock(return_value=Response(payload)))
        serialized = json.dumps(result)
        for secret in (self.key, quote(self.key, safe=''), quote_plus(self.key)):
            self.assertNotIn(secret, serialized)
        self.assertEqual(code, 0)
        self.assertIn('[REDACTED]', serialized)

    def test_http_errors_never_echo_exception_urls_or_bodies(self):
        for status, expected in [(401, 'BLOCKED_CAPABILITY'), (403, 'BLOCKED_CAPABILITY'), (404, 'NOT_FOUND'), (302, 'FAILED')]:
            with self.subTest(status=status):
                error = HTTPError('https://api.example?key=' + self.key, status, self.key, {}, io.BytesIO(self.key.encode()))
                opener = Mock(side_effect=error)
                result, _ = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener)
                self.assertEqual(result['status'], expected)
                self.assertEqual(opener.call_count, 1)
                self.assertNotIn(self.key, json.dumps(result))

    def test_rate_limit_and_server_errors_retry_at_most_three_attempts(self):
        error = lambda status: HTTPError('https://api.example?key=' + self.key, status, self.key, {}, io.BytesIO())
        opener = Mock(side_effect=[error(429), error(503), Response({'ip_str': '8.8.8.8'})])
        sleep = Mock()
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener, sleep=sleep)
        self.assertEqual((result['attempts'], code), (3, 0))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        opener = Mock(side_effect=[error(429), error(429), error(429)])
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener, sleep=Mock())
        self.assertEqual((result['status'], code), ('RETRY_EXHAUSTED', 75))
        self.assertEqual(opener.call_count, 3)

    def test_transport_failure_is_bounded_and_sanitized(self):
        opener = Mock(side_effect=URLError('https://api.example?key=' + self.key))
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener)
        self.assertEqual((result['status'], code), ('FAILED', 1))
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn(self.key, json.dumps(result))

    def test_long_retry_after_returns_cooldown_instead_of_retrying_early(self):
        error = HTTPError('https://api.example', 429, '', {'Retry-After': '60'}, io.BytesIO())
        opener, sleep = Mock(side_effect=error), Mock()
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener, sleep=sleep)
        self.assertEqual((result['status'], code), ('RETRY_EXHAUSTED', 75))
        self.assertEqual(result['retry_after_seconds'], 60)
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_http_date_retry_after_does_not_retry_early(self):
        error = HTTPError('https://api.example', 429, '', {'Retry-After': 'Wed, 21 Oct 2037 07:28:00 GMT'}, io.BytesIO())
        opener, sleep = Mock(side_effect=error), Mock()
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener, sleep=sleep)
        self.assertEqual((result['status'], code), ('RETRY_EXHAUSTED', 75))
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_http_success_requires_a_minimal_provider_record(self):
        cases = [
            ('vt', 'file', 'a'*64, {}),
            ('vt', 'file', 'a'*64, {'data': []}),
            ('vt', 'file', 'a'*64, {'data': {'id': 'a'*64}}),
            ('vt', 'file', 'a'*64, {'data': {'id': 'b'*64, 'type': 'file'}}),
            ('vt', 'file', 'a'*64, {'data': {'id': 'a'*64, 'type': 'domain'}}),
            ('shodan', 'host', '8.8.8.8', {}),
            ('shodan', 'host', '8.8.8.8', {'ip_str': 'not-an-ip'}),
            ('shodan', 'host', '8.8.8.8', {'ip_str': 134744072}),
            ('shodan', 'host', '8.8.8.8', {'ip_str': '1.1.1.1'}),
            ('shodan', 'host', '8.8.8.8', {'ip_str': '8.8.8.8', 'ports': '53'}),
            ('maps', 'geocode', 'Washington', {'status': 'OK'}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': []}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': [{}]}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': [{'place_id': 'fixture'}]}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': [{'geometry': {'location': {'lat': '37', 'lng': 0}}}]}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': [{'geometry': {'location': {'lat': 200, 'lng': 0}}}]}),
            ('maps', 'geocode', 'Washington', {'status': 'OK', 'results': ['not-a-result']}),
            ('maps', 'geocode', 'Washington', {'status': 'ZERO_RESULTS'}),
            ('maps', 'geocode', 'Washington', {'status': 'ZERO_RESULTS', 'results': 'none'}),
        ]
        for provider, resource, value, payload in cases:
            with self.subTest(provider=provider, payload=payload):
                result, code = api.lookup(provider, resource, value, env=self.env(provider), opener=Mock(return_value=Response(payload)))
                self.assertEqual((result['status'], code), ('FAILED', 1))
                self.assertTrue(result['live_response_received'])

    def test_invalid_json_response_and_bounds_fail_cleanly(self):
        response = Response({}); response.seek(0); response.write(b'bad-json'); response.seek(0)
        result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=Mock(return_value=response))
        self.assertEqual((result['status'], code), ('FAILED', 1))
        for options in ({'timeout': 0}, {'timeout': float('nan')}, {'max_attempts': 4}):
            opener = Mock()
            result, code = api.lookup('shodan', 'host', '8.8.8.8', env=self.env('shodan'), opener=opener, **options)
            self.assertEqual(code, 64)
            opener.assert_not_called()
        result, code = api.lookup('maps', 'geocode', 'Washington', env=self.env('maps'), opener=Mock(return_value=Response({'status': []})))
        self.assertEqual((result['status'], code), ('FAILED', 1))

    def test_maps_body_errors_are_not_successful_geocodes(self):
        for status, expected, code in [('REQUEST_DENIED', 'BLOCKED_CAPABILITY', 78),
                                      ('OVER_QUERY_LIMIT', 'RETRY_EXHAUSTED', 75),
                                      ('ZERO_RESULTS', 'NOT_FOUND', 0), ('INVALID_REQUEST', 'FAILED', 1)]:
            opener = Mock(return_value=Response({'status': status, 'error_message': self.key, 'results': []}))
            result, actual = api.lookup('maps', 'geocode', 'Washington', env=self.env('maps'), opener=opener)
            self.assertEqual((result['status'], actual), (expected, code))
            self.assertNotIn(self.key, json.dumps(result))
            self.assertEqual(opener.call_count, 1)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://untrusted.example'))

    def test_cli_missing_key_returns_structured_block_without_network(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(api, 'build_opener') as opener, contextlib.redirect_stdout(io.StringIO()) as output:
            code = api.main(['shodan', 'host', '8.8.8.8'])
        self.assertEqual(code, 78)
        self.assertEqual(json.loads(output.getvalue())['status'], 'BLOCKED_CAPABILITY')
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
