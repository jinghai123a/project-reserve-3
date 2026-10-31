"""Bounded GET adapters for existing VirusTotal, Shodan and Maps accounts.

Official contracts verified 2026-10-26:
https://docs.virustotal.com/reference/file-info
https://docs.virustotal.com/reference/domain-info
https://docs.virustotal.com/reference/ip-info
https://developer.shodan.io/api
https://developers.google.com/maps/documentation/geocoding/guides-v3/requests-geocoding

VT domain/IP lookups may trigger provider scanning and public dataset inclusion.
No uploads, scan submissions, account creation or payment operations are exposed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


KEY_NAMES = {'vt': 'VIRUSTOTAL_API_KEY', 'shodan': 'SHODAN_API_KEY', 'maps': 'GOOGLE_MAPS_API_KEY'}
MAX_BODY = 4 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    """Never forward API credentials to a redirect destination."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _key(provider, env):
    name = KEY_NAMES[provider]
    value, filename = env.get(name, ''), env.get(name + '_FILE', '')
    if value and filename:
        raise ValueError('configure exactly one key source: ' + name + ' or ' + name + '_FILE')
    if filename:
        try:
            with Path(filename).open(encoding='utf-8') as stream:
                value = stream.read(8193)
        except (OSError, UnicodeError, ValueError):
            raise ValueError('key file is unreadable') from None
    value = value.strip()
    if not value:
        raise ValueError('missing key: set ' + name + ' or ' + name + '_FILE')
    if len(value) > 8192 or any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ValueError('key has invalid length or characters')
    return value


def _value(provider, resource, value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError('a nonempty value without control characters is required')
    value = value.strip()
    if provider == 'vt' and resource == 'file':
        if not re.fullmatch(r'[a-fA-F0-9]{64}', value):
            raise ValueError('file lookup requires a SHA-256 hash, not a file path')
        return value.lower()
    if (provider, resource) in {('vt', 'ip'), ('shodan', 'host')}:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError('a literal public IP address is required') from None
        if not address.is_global or '%' in value:
            raise ValueError('only public IP indicators are supported')
        return str(address)
    if provider == 'vt' and resource == 'domain':
        try:
            domain = value.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('invalid domain name') from None
        labels = domain.split('.')
        if (len(domain) > 253 or len(labels) < 2 or labels[-1].isdigit()
                or labels[-1] in {'local', 'localhost', 'internal', 'invalid', 'test'}
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
            raise ValueError('a public domain name without scheme, port or path is required')
        return domain
    if provider == 'maps' and resource == 'geocode':
        if len(value) > 2000:
            raise ValueError('address exceeds 2000 characters')
        return value
    raise ValueError('unsupported provider/resource combination')


def _sanitize(value, key):
    if isinstance(value, dict):
        return {_sanitize(str(name), key): _sanitize(item, key) for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, str):
        for secret in sorted({key, quote(key, safe=''), quote_plus(key)}, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)([?&](?:key|api_key|apikey)=)[^&#\s]*', r'\1[REDACTED]', value)
    return value


def _geocode_result(value):
    geometry = value.get('geometry') if isinstance(value, dict) else None
    location = geometry.get('location') if isinstance(geometry, dict) else None
    return isinstance(location, dict) and all(
        isinstance(location.get(axis), (int, float)) and not isinstance(location[axis], bool)
        and math.isfinite(location[axis]) and -limit <= location[axis] <= limit
        for axis, limit in (('lat', 90), ('lng', 180)))


def lookup(provider, resource, value, *, env=None, timeout=15, max_attempts=3,
           allow_public_submission=False, opener=None, sleep=time.sleep):
    """Return (sanitized evidence, exit code); never log request URLs or exceptions."""
    evidence = {'schema_version': 1, 'provider': provider, 'resource': resource,
                'method': 'GET', 'attempts': 0, 'live_response_received': False,
                'observed_at': datetime.now(timezone.utc).isoformat()}
    def finish(status, code, **fields):
        return {**evidence, 'status': status, **fields}, code
    try:
        if provider not in KEY_NAMES:
            raise ValueError('unsupported provider')
        normalized = _value(provider, resource, value)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('timeout must be positive and at most 60 seconds')
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be between 1 and 3')
    except ValueError as error:
        return finish('INVALID_INPUT', 64, reason=str(error))
    if provider == 'vt' and resource in {'domain', 'ip'} and not allow_public_submission:
        return finish('BLOCKED_CAPABILITY', 78, reason='VT may scan queried indicators and include them in its public dataset; explicitly allow public submission for a non-sensitive authorized indicator')
    try:
        key = _key(provider, os.environ if env is None else env)
    except ValueError as error:
        return finish('BLOCKED_CAPABILITY', 78, reason=str(error))
    headers = {'Accept': 'application/json', 'User-Agent': 'CodexCloudKit-intelligence/1'}
    if provider == 'vt':
        endpoint = 'https://www.virustotal.com/api/v3/' + {'file': 'files', 'domain': 'domains', 'ip': 'ip_addresses'}[resource] + '/' + quote(normalized, safe='')
        url = endpoint
        headers['x-apikey'] = key
    elif provider == 'shodan':
        endpoint = 'https://api.shodan.io/shodan/host/' + quote(normalized, safe='')
        url = endpoint + '?' + urlencode({'key': key})
    else:
        endpoint = 'https://maps.googleapis.com/maps/api/geocode/json'
        url = endpoint + '?' + urlencode({'address': normalized, 'key': key})
    evidence.update(endpoint=_sanitize(endpoint, key), value=_sanitize(normalized, key))
    transport = opener or build_opener(NoRedirect()).open
    for attempt in range(1, max_attempts + 1):
        evidence['attempts'] = attempt
        try:
            with transport(Request(url, headers=headers, method='GET'), timeout=timeout) as response:
                evidence.update(http_status=response.status, live_response_received=True)
                body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                return finish('FAILED', 1, reason='provider response exceeded size limit')
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return finish('FAILED', 1, reason='provider response must be a JSON object')
        except HTTPError as error:
            status = error.code
            retry_after = error.headers.get('Retry-After', '') if error.headers else ''
            error.close()
            evidence.update(http_status=status, live_response_received=True)
            if status == 429 or 500 <= status <= 599:
                if attempt < max_attempts:
                    if retry_after and not re.fullmatch(r'\d{1,10}', retry_after):
                        return finish('RETRY_EXHAUSTED', 75, reason='provider requested a cooldown that must be scheduled before retry')
                    delay = max(2 ** (attempt - 1), int(retry_after)) if re.fullmatch(r'\d{1,10}', retry_after) else 2 ** (attempt - 1)
                    if delay > 30:
                        return finish('RETRY_EXHAUSTED', 75, reason='provider requested a longer cooldown', retry_after_seconds=delay)
                    sleep(delay)
                    continue
                return finish('RETRY_EXHAUSTED', 75, reason='provider temporarily unavailable or rate limited')
            if status in (401, 403):
                return finish('BLOCKED_CAPABILITY', 78, reason='provider rejected credentials or account access')
            return finish('NOT_FOUND' if status == 404 else 'FAILED', 0 if status == 404 else 1,
                          reason='no provider record' if status == 404 else 'provider HTTP request failed')
        except (URLError, OSError, TimeoutError):
            return finish('FAILED', 1, reason='provider transport failed or timed out')
        except (ValueError, UnicodeError, RecursionError):
            return finish('FAILED', 1, reason='provider returned invalid JSON')
        data = _sanitize(payload, key)
        if provider == 'maps':
            status = payload.get('status')
            if not isinstance(status, str):
                return finish('FAILED', 1, reason='provider geocoding status is missing or invalid')
            if status == 'ZERO_RESULTS':
                if payload.get('results') != []:
                    return finish('FAILED', 1, reason='provider zero-results response is malformed')
                return finish('NOT_FOUND', 0, data=data)
            if status in {'REQUEST_DENIED', 'OVER_DAILY_LIMIT'}:
                return finish('BLOCKED_CAPABILITY', 78, data=data)
            if status in {'OVER_QUERY_LIMIT', 'UNKNOWN_ERROR'}:
                return finish('RETRY_EXHAUSTED', 75, data=data)
            if status != 'OK':
                return finish('FAILED', 1, reason='provider did not report successful geocoding', data=data)
            results = payload.get('results')
            if not isinstance(results, list) or not results or not all(_geocode_result(item) for item in results):
                return finish('FAILED', 1, reason='provider geocoding results are missing or malformed')
        elif 'error' in payload:
            return finish('FAILED', 1, reason='provider returned an error', data=data)
        elif provider == 'vt':
            record = payload.get('data')
            expected_type = {'file': 'file', 'domain': 'domain', 'ip': 'ip_address'}[resource]
            if not isinstance(record, dict) or record.get('id') != normalized or record.get('type') != expected_type:
                return finish('FAILED', 1, reason='provider report identity or object type is missing or mismatched')
        elif provider == 'shodan':
            try:
                matches = isinstance(payload.get('ip_str'), str) and str(ipaddress.ip_address(payload['ip_str'])) == normalized
            except ValueError:
                matches = False
            if not matches or ('ports' in payload and not isinstance(payload['ports'], list)):
                return finish('FAILED', 1, reason='provider host identity or ports are malformed')
        return finish('DONE', 0, data=data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider', choices=tuple(KEY_NAMES))
    parser.add_argument('resource', help='vt: file/domain/ip; shodan: host; maps: geocode')
    parser.add_argument('value', help='public indicator or address; never an API key')
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--max-attempts', type=int, default=3)
    parser.add_argument('--allow-public-submission', action='store_true',
                        help='VT domain/IP queries may trigger scanning and public dataset inclusion; confirm this non-sensitive indicator may be published')
    args = parser.parse_args(argv)
    result, code = lookup(args.provider, args.resource, args.value, timeout=args.timeout,
                          max_attempts=args.max_attempts, allow_public_submission=args.allow_public_submission)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
