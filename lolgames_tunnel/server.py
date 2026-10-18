import argparse
import asyncio
import calendar
import html
import json
import os
import random
import string
import sys
import time
import threading
import urllib.error
import urllib.request

from .dashboard import dashboard_html

from .common import (
    BROKER_PORT,
    LOCAL_REGISTRY_PORT,
    CONTROL_PORT,
    DOMAIN,
    b64,
    original_dst,
    parse_host,
    raise_buffer_limits,
    rand_name,
    recv,
    send,
    ub64,
)

REG = {}
CTRL_BY_KEY = {}
META_BY_KEY = {}


def known_domains():
    """Primary tunnel domain plus optional legacy domains (TUNNEL_DOMAINS=comma list)."""
    extra = [d.strip() for d in os.environ.get('TUNNEL_DOMAINS', '').split(',') if d.strip()]
    return [DOMAIN] + [d for d in extra if d != DOMAIN]


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def peer_is_loopback(writer):
    """Registry pages are only served to the local Caddy proxy, never to direct
    public connections that forge a broker.agentsweb.space Host header."""
    try:
        peer = writer.get_extra_info('peername')
    except Exception:
        return False
    if not peer:
        return False
    return str(peer[0]).startswith('127.') or peer[0] == '::1'


BROKER_API_TOKEN = os.environ.get('BROKER_API_TOKEN', '').strip()
BROKER_HOST = f'broker.{DOMAIN}'
CONN_QUEUE_MAX = int(os.environ.get('LOLGAMES_TUNNEL_CONN_QUEUE', '512') or '512')
MAX_CONNS_PER_REGISTRATION = int(os.environ.get('LOLGAMES_TUNNEL_MAX_CONNS', '128') or '128')
CONN_STALL_TIMEOUT = float(os.environ.get('LOLGAMES_TUNNEL_STALL_TIMEOUT', '15') or '15')
FLOW_PAUSE_AT = max(1, CONN_QUEUE_MAX // 2)
FLOW_RESUME_AT = max(0, CONN_QUEUE_MAX // 8)
HISTORY_FILE = os.environ.get('LOLGAMES_TUNNEL_HISTORY_FILE', '/var/log/lolgames-tunnel/history.jsonl')
PROBE_INTERVAL = float(os.environ.get('LOLGAMES_TUNNEL_PROBE_INTERVAL', '30') or '30')
PROBE_TIMEOUT = float(os.environ.get('LOLGAMES_TUNNEL_PROBE_TIMEOUT', '8') or '8')
FIXED_PROBE_URLS = tuple(
    url.strip().rstrip('/')
    for url in os.environ.get('LOLGAMES_TUNNEL_PROBE_URLS', '').split(',')
    if url.strip()
)
FRPS_API_URL = os.environ.get('LOLGAMES_TUNNEL_FRPS_API_URL', 'http://127.0.0.1:17500/api/proxy/http').strip()
HISTORY_LOCK = threading.Lock()
HISTORY_BUCKET_SECONDS = 60
HISTORY_BUCKET_COUNT = 90
HISTORY_WINDOW_SECONDS = HISTORY_BUCKET_SECONDS * HISTORY_BUCKET_COUNT
HISTORY_READ_MAX_BYTES = int(os.environ.get('LOLGAMES_TUNNEL_HISTORY_READ_MAX_BYTES', str(32 * 1024 * 1024)))
PROBE_STALE_SECONDS = int(os.environ.get('LOLGAMES_TUNNEL_PROBE_STALE_SECONDS', str(max(90, int(PROBE_INTERVAL * 3)))))
PROCESS_STARTED_AT = utc_now()
CURRENT_FRP_WORKER_URLS = set()
LAST_TARGET_STATES = {}
LAST_FRP_DISCOVERY_AT = ''
LAST_PROBE_CYCLE_AT = ''


def history_event(event_type, level='info', **fields):
    event = {'time': utc_now(), 'type': event_type, 'level': level, **fields}
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or '.', exist_ok=True)
        line = json.dumps(event, separators=(',', ':')) + '\n'
        with HISTORY_LOCK:
            with open(HISTORY_FILE, 'a', encoding='utf-8') as stream:
                stream.write(line)
                stream.flush()
    except Exception as exc:
        print(f'WARNING: unable to append broker history: {exc}', file=sys.stderr, flush=True)


def probe_url(url):
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'agentsweb-broker-probe/2'})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            response.read(256)
            return {
                'ok': 200 <= response.status < 400,
                'status': response.status,
                'error': '',
                'latency_ms': round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            'ok': False,
            'status': exc.code,
            'error': str(exc.reason),
            'latency_ms': round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            'ok': False,
            'status': 0,
            'error': str(exc)[:240],
            'latency_ms': round((time.monotonic() - started) * 1000),
        }


async def probe_registrations():
    """Persist HTTPS health evidence for persistent targets and active workers."""
    global CURRENT_FRP_WORKER_URLS, LAST_FRP_DISCOVERY_AT, LAST_PROBE_CYCLE_AT
    while True:
        targets = {
            url: {
                'url': url,
                'subdomain': url.split('://', 1)[-1].split('/', 1)[0],
                'public_port': 0,
                'source': 'configured',
                'required': True,
            }
            for url in FIXED_PROBE_URLS
        }
        try:
            discovered = set(await asyncio.to_thread(discover_frp_worker_urls))
            CURRENT_FRP_WORKER_URLS = discovered
            LAST_FRP_DISCOVERY_AT = utc_now()
            for status_url in discovered:
                if status_url in targets:
                    continue
                targets[status_url] = {
                    'url': status_url,
                    'subdomain': status_url.split('://', 1)[-1].split('/', 1)[0],
                    'public_port': 0,
                    'source': 'frp',
                    'required': False,
                }
        except Exception as exc:
            history_event('frp_probe_discovery_error', level='error', error=str(exc)[:240])
            for status_url in CURRENT_FRP_WORKER_URLS:
                targets[status_url] = {
                    'url': status_url,
                    'subdomain': status_url.split('://', 1)[-1].split('/', 1)[0],
                    'public_port': 0,
                    'source': 'frp',
                    'required': False,
                }
        for target in targets.values():
            result = await asyncio.to_thread(probe_url, target['url'])
            record_probe_result(target, result)
        LAST_PROBE_CYCLE_AT = utc_now()
        await asyncio.sleep(PROBE_INTERVAL)


def record_probe_result(target, result):
    """Persist a probe and emit a durable event only when health changes."""
    url = target['url']
    previous = LAST_TARGET_STATES.get(url)
    current = bool(result['ok'])
    fields = {
        'scheme': 'https',
        'url': url,
        'subdomain': target['subdomain'],
        'public_port': target['public_port'],
        'source': target['source'],
        'required': bool(target.get('required')),
        **result,
    }
    history_event('http_probe', level='ok' if current else 'error', **fields)
    if (previous is None and not current) or (previous is not None and previous != current):
        history_event(
            'target_recovered' if current else 'target_down',
            level='ok' if current else 'error',
            **fields,
        )
    LAST_TARGET_STATES[url] = current


def discover_frp_worker_urls():
    """Return live Worker Agents status URLs from frps, not SSH name guesses."""
    request = urllib.request.Request(FRPS_API_URL, headers={'User-Agent': 'agentsweb-broker-discovery/1'})
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
        payload = json.load(response)
    urls = set()
    for proxy in payload.get('proxies') or []:
        if proxy.get('status') != 'online':
            continue
        proxy_name = str(proxy.get('name') or '').strip().lower()
        conf = proxy.get('conf') or {}
        hosts = {
            str(host or '').strip().lower().rstrip('.')
            for host in conf.get('customDomains') or []
        }
        for host in hosts:
            bare_host = host.replace(f'-1456.{DOMAIN}', f'.{DOMAIN}')
            if bare_host != host and bare_host in hosts:
                continue
            if host.endswith(f'.{DOMAIN}') and (
                host.endswith(f'-worker-agents.{DOMAIN}')
                or proxy_name.endswith('-worker-agents-1456')
            ):
                urls.add(f'https://{host}/api/status')
    return sorted(urls)


def bearer_token(initial):
    """Return the Bearer token from an HTTP request head, or ''."""
    try:
        for raw in initial.decode('iso-8859-1', 'ignore').splitlines():
            if raw.lower().startswith('authorization:'):
                parts = raw.split(None, 2)
                if len(parts) >= 2 and parts[1].lower() == 'bearer':
                    return (parts[2] if len(parts) >= 3 else '').strip()
    except Exception:
        pass
    return ''


def request_header(initial, name):
    prefix = name.lower() + ':'
    try:
        for raw in initial.decode('iso-8859-1', 'ignore').splitlines():
            if raw.lower().startswith(prefix):
                return raw.split(':', 1)[1].strip()
    except Exception:
        pass
    return ''


def seconds_since(value):
    if not value:
        return 0
    try:
        parsed = time.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return max(0, int(time.time() - calendar.timegm(parsed)))
    except Exception:
        return 0


def human_duration(seconds):
    seconds = int(seconds or 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def iso_from_ts(timestamp):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(timestamp))


def read_broker_history(window_seconds=HISTORY_WINDOW_SECONDS + 300, now=None):
    """Read recent history by timestamp, bounded by bytes rather than line count."""
    cutoff = int(time.time() if now is None else now) - max(1, int(window_seconds))
    events = []
    try:
        with HISTORY_LOCK:
            with open(HISTORY_FILE, 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                position = stream.tell()
                chunks = []
                bytes_read = 0
                found_boundary = False
                while position > 0 and bytes_read < HISTORY_READ_MAX_BYTES and not found_boundary:
                    size = min(64 * 1024, position)
                    position -= size
                    stream.seek(position)
                    chunk = stream.read(size)
                    chunks.append(chunk)
                    bytes_read += len(chunk)
                    for raw_line in chunk.splitlines():
                        try:
                            candidate = json.loads(raw_line.decode('utf-8'))
                        except (UnicodeDecodeError, TypeError, json.JSONDecodeError):
                            continue
                        timestamp = event_timestamp(candidate) if isinstance(candidate, dict) else None
                        if timestamp is not None and timestamp < cutoff:
                            found_boundary = True
                            break
        lines = b''.join(reversed(chunks)).splitlines()
        for raw_line in lines:
            try:
                event = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict) and (event_timestamp(event) or 0) >= cutoff:
                events.append(event)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return [], str(exc)
    return list(events), ''


def event_timestamp(event):
    value = event.get('time')
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError, OverflowError):
        return None


def build_uptime_history(events=None, bucket_count=HISTORY_BUCKET_COUNT, now=None):
    """Build a core SLO and a separate active-worker view from probe evidence."""
    if events is None:
        events, read_error = read_broker_history()
    else:
        read_error = ''
    now = int(time.time() if now is None else now)
    end = now - (now % HISTORY_BUCKET_SECONDS) + HISTORY_BUCKET_SECONDS
    start = end - (bucket_count * HISTORY_BUCKET_SECONDS)
    buckets = []
    for index in range(bucket_count):
        bucket_start = start + index * HISTORY_BUCKET_SECONDS
        buckets.append({
            'start': iso_from_ts(bucket_start),
            'end': iso_from_ts(bucket_start + HISTORY_BUCKET_SECONDS),
            'level': 'empty',
            'checks': 0,
            'healthyChecks': 0,
            'failedChecks': 0,
        })

    core_probe_count = 0
    healthy_core_probe_count = 0
    target_stats = {}
    recent_events = []
    for event in events:
        timestamp = event_timestamp(event)
        if timestamp is None:
            continue
        if timestamp < start or timestamp >= end:
            continue
        event_type = str(event.get('type') or '')
        if event_type == 'http_probe' and event.get('source') not in {'configured', 'frp'}:
            # Pre-migration probes guessed Worker Agents URLs from SSH names.
            # Keep those raw records in /api/events, but exclude them from the
            # canonical uptime calculation because the guessed hosts can 404
            # while the real FRP worker is healthy under a different name.
            continue
        if event_type == 'http_probe':
            ok = bool(event.get('ok'))
            source = str(event.get('source') or '')
            if source == 'configured':
                core_probe_count += 1
                healthy_core_probe_count += int(ok)
            url = str(event.get('url') or '')
            if url:
                target = target_stats.setdefault(url, {
                    'url': url,
                    'source': source,
                    'required': source == 'configured',
                    'checks': 0,
                    'healthyChecks': 0,
                    'failedChecks': 0,
                    'lastCheckedAt': '',
                    'lastStatus': 0,
                    'lastLatencyMs': None,
                    'lastError': '',
                    'ok': False,
                })
                target['checks'] += 1
                target['healthyChecks'] += int(ok)
                target['failedChecks'] += int(not ok)
                if not target['lastCheckedAt'] or str(event.get('time') or '') >= target['lastCheckedAt']:
                    target['lastCheckedAt'] = str(event.get('time') or '')
                    target['lastStatus'] = int(event.get('status') or 0)
                    target['lastLatencyMs'] = event.get('latency_ms')
                    target['lastError'] = str(event.get('error') or '')
                    target['ok'] = ok

        bucket = buckets[min(bucket_count - 1, max(0, (timestamp - start) // HISTORY_BUCKET_SECONDS))]
        if event_type == 'http_probe' and event.get('source') == 'configured':
            ok = bool(event.get('ok'))
            bucket['checks'] += 1
            bucket['healthyChecks'] += int(ok)
            bucket['failedChecks'] += int(not ok)
            if not ok:
                bucket['level'] = 'error'
            elif bucket['level'] == 'empty':
                bucket['level'] = 'ok'
        recent_events.append(event)

    targets = []
    for target in target_stats.values():
        if target['url'] in FIXED_PROBE_URLS:
            target['source'] = 'configured'
            target['required'] = True
        target['uptimePercent'] = round(
            target['healthyChecks'] / target['checks'] * 100, 2
        ) if target['checks'] else 0.0
        target['name'] = target_name(target['url'], target['source'])
        target['type'] = 'core' if target['source'] == 'configured' else 'worker'
        target['active'] = target['source'] == 'configured' or target['url'] in CURRENT_FRP_WORKER_URLS
        target['lastCheckAgeSeconds'] = seconds_since(target['lastCheckedAt'])
        targets.append(target)
    targets.sort(key=lambda target: (target['type'] != 'core', not target['ok'], target['name']))
    core_targets = [target for target in targets if target['type'] == 'core']
    worker_targets = [target for target in targets if target['type'] == 'worker' and target['active']]
    incidents = []
    for event in recent_events:
        if event.get('type') not in {'target_down', 'target_recovered'}:
            continue
        incidents.append({
            'time': event.get('time', ''),
            'type': event.get('type'),
            'name': target_name(str(event.get('url') or ''), str(event.get('source') or '')),
            'detail': f"HTTP {int(event.get('status') or 0) or 'error'} · {int(event.get('latency_ms') or 0)} ms",
        })

    return {
        'ok': not bool(read_error),
        'historyFile': HISTORY_FILE,
        'readError': read_error,
        'bucketSeconds': HISTORY_BUCKET_SECONDS,
        'bucketCount': bucket_count,
        'windowStart': buckets[0]['start'] if buckets else iso_from_ts(start),
        'windowEnd': buckets[-1]['end'] if buckets else iso_from_ts(end),
        'uptimePercent': round(healthy_core_probe_count / core_probe_count * 100, 2) if core_probe_count else 0.0,
        'probeCount': core_probe_count,
        'healthyProbeCount': healthy_core_probe_count,
        'failedProbeCount': core_probe_count - healthy_core_probe_count,
        'buckets': buckets,
        'targets': targets,
        'coreTargets': core_targets,
        'workerTargets': worker_targets,
        'incidents': incidents[-50:][::-1],
        'events': recent_events[-200:],
    }


def target_name(url, source=''):
    host = str(url or '').split('://', 1)[-1].split('/', 1)[0]
    core_names = {
        f'a2.{DOMAIN}': 'A2',
        f'dev.{DOMAIN}': 'Development',
        f'guest.{DOMAIN}': 'Guest',
    }
    if host in core_names:
        return core_names[host]
    if source == 'frp' and host.endswith(f'-worker-agents.{DOMAIN}'):
        host = host[: -len(f'-worker-agents.{DOMAIN}')]
    return host or 'Unknown target'


def public_history_summary(history):
    target_fields = (
        'url', 'name', 'type', 'ok', 'uptimePercent', 'checks', 'healthyChecks',
        'failedChecks', 'lastCheckedAt', 'lastCheckAgeSeconds', 'lastStatus', 'lastLatencyMs',
    )
    clean = lambda target: {key: target.get(key) for key in target_fields}
    return {
        'windowStart': history['windowStart'],
        'windowEnd': history['windowEnd'],
        'bucketSeconds': history['bucketSeconds'],
        'uptimePercent': history['uptimePercent'],
        'probeCount': history['probeCount'],
        'healthyProbeCount': history['healthyProbeCount'],
        'failedProbeCount': history['failedProbeCount'],
        'buckets': [{key: bucket[key] for key in ('start', 'end', 'level', 'checks', 'healthyChecks', 'failedChecks')} for bucket in history['buckets']],
        'coreTargets': [clean(target) for target in history['coreTargets']],
        'workerTargets': [clean(target) for target in history['workerTargets']],
        'incidents': history['incidents'][:20],
    }


def broker_status_snapshot(events=None, read_error=''):
    if events is None:
        events, read_error = read_broker_history()
    history = build_uptime_history(events)
    last_event = events[-1] if events else None
    probes = [event for event in events if event.get('type') == 'http_probe' and event.get('source') == 'configured']
    last_probe = probes[-1] if probes else None
    active_connections = sum(int(meta.get('active_connections') or 0) for meta in META_BY_KEY.values())
    required_urls = set(FIXED_PROBE_URLS) or {target['url'] for target in history['coreTargets']}
    latest_by_url = {target['url']: target for target in history['coreTargets']}
    failed_required = sorted(url for url in required_urls if url in latest_by_url and not latest_by_url[url]['ok'])
    stale_required = sorted(url for url in required_urls if url not in latest_by_url or latest_by_url[url]['lastCheckAgeSeconds'] > PROBE_STALE_SECONDS)
    probes_fresh = bool(required_urls) and not stale_required
    required_healthy = bool(required_urls) and not failed_required and all(url in latest_by_url for url in required_urls)
    process_ok = not bool(read_error)
    ok = process_ok and probes_fresh and required_healthy
    failure_reasons = []
    if not process_ok:
        failure_reasons.append('Broker history is unavailable')
    if stale_required:
        failure_reasons.append(f'{len(stale_required)} required probe target(s) are stale')
    if failed_required:
        failure_reasons.append(f'{len(failed_required)} required service(s) are failing')
    return {
        'ok': ok,
        'service': 'agentsweb-broker',
        'domain': DOMAIN,
        'updatedAt': utc_now(),
        'startedAt': PROCESS_STARTED_AT,
        'uptimeSeconds': seconds_since(PROCESS_STARTED_AT),
        'uptimeText': human_duration(seconds_since(PROCESS_STARTED_AT)),
        'registrations': len(META_BY_KEY),
        'activeConnections': active_connections,
        'brokerProcessOk': process_ok,
        'probesFresh': probes_fresh,
        'requiredTargetsHealthy': required_healthy,
        'failureReasons': failure_reasons,
        'failedRequiredTargets': failed_required,
        'staleRequiredTargets': stale_required,
        'lastEventAt': (last_event or {}).get('time', ''),
        'lastProbeAt': (last_probe or {}).get('time', ''),
        'lastProbeAgeSeconds': seconds_since((last_probe or {}).get('time')),
        'lastProbeCycleAt': LAST_PROBE_CYCLE_AT,
        'lastWorkerDiscoveryAt': LAST_FRP_DISCOVERY_AT,
        'probeTargets': len(history['coreTargets']),
        'activeWorkers': len(history['workerTargets']),
        'failedTargets': len(failed_required),
        'uptimePercent': history['uptimePercent'],
        'probeCount': history['probeCount'],
        'summary': public_history_summary(history),
    }


def broker_events_snapshot(events=None):
    if events is None:
        events, read_error = read_broker_history()
    else:
        read_error = ''
    return {
        'ok': not bool(read_error),
        'updatedAt': utc_now(),
        'historyFile': HISTORY_FILE,
        'readError': read_error,
        'count': min(200, len(events)),
        'events': events[-200:],
    }


def registration_urls(subdomain, public_port):
    """Return the human web link and raw SSH link for a broker registration.

    The Python broker carries SSH only. Worker HTTP is published separately by
    FRP, but runner registrations have a stable paired Worker Agents hostname
    that is useful from the registry UI.
    """
    ssh_url = f'ssh://{subdomain}.{DOMAIN}:{public_port}'
    if subdomain.endswith('-ssh'):
        worker_prefix = subdomain[:-4]
        return f'https://{worker_prefix}-worker-agents.{DOMAIN}', ssh_url
    return ssh_url, ssh_url


def parse_request(buf):
    try:
        first = buf.decode('iso-8859-1', 'ignore').splitlines()[0]
        parts = first.split()
        if len(parts) >= 2 and parts[0].isalpha():
            return parts[0].upper(), parts[1].split('?', 1)[0]
    except Exception:
        pass
    return '', ''


def registration_snapshot():
    rows = []
    for (subdomain, public_port), meta in sorted(META_BY_KEY.items()):
        started_at = meta.get('started_at', '')
        uptime_seconds = seconds_since(started_at)
        url, ssh_url = registration_urls(subdomain, public_port)
        rows.append({
            **meta,
            'subdomain': subdomain,
            'public_port': public_port,
            'uptime_seconds': uptime_seconds,
            'uptime_text': human_duration(uptime_seconds),
            'url': url,
            'ssh_url': ssh_url,
        })
    return {
        'ok': True,
        'updated_at': utc_now(),
        'domain': DOMAIN,
        'count': len(rows),
        'registrations': rows,
    }


def http_response(status, content_type, body):
    data = body.encode('utf-8')
    reason = {
        200: 'OK',
        401: 'Unauthorized',
        404: 'Not Found',
        503: 'Service Unavailable',
        500: 'Internal Server Error',
    }.get(status, 'Error')
    return (
        f'HTTP/1.1 {status} {reason}\r\n'
        f'content-type: {content_type}\r\n'
        'cache-control: no-store, max-age=0\r\n'
        f'content-length: {len(data)}\r\n'
        'connection: close\r\n'
        '\r\n'
    ).encode('utf-8') + data


def broker_html():
    return dashboard_html()

async def write_broker_response(writer, path):
    events, read_error = read_broker_history()
    status_payload = broker_status_snapshot(events, read_error)
    payloads = {
        '/api/status': status_payload,
        '/api/registrations': registration_snapshot(),
        '/api/history': build_uptime_history(events),
        '/api/events': broker_events_snapshot(events),
    }
    if path in payloads:
        body = json.dumps(payloads[path], separators=(',', ':')) + '\n'
        response_status = 503 if path == '/api/status' and not status_payload['ok'] else 200
        writer.write(http_response(response_status, 'application/json; charset=utf-8', body))
    elif path in {'', '/'}:
        writer.write(http_response(200, 'text/html; charset=utf-8', broker_html()))
    else:
        writer.write(http_response(404, 'application/json; charset=utf-8', '{"error":"Not found"}\n'))
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def handle_control(reader, writer):
    try:
        hello = await recv(reader)
    except (ValueError, UnicodeDecodeError):
        # Garbage on the control port (scanners, stray clients). Close quietly
        # instead of letting an unhandled exception churn the handler task.
        hello = None
    if not hello or hello.get('type') != 'register':
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    subdomain = hello.get('subdomain') or rand_name()
    for d in known_domains():
        suffix = '.' + d
        if subdomain.endswith(suffix):
            subdomain = subdomain[: -len(suffix)]
            break
    subdomain = subdomain.lower()
    port = int(hello.get('public_port', 0))
    target_port = int(hello.get('display_port') or 0)
    if target_port not in (22, 2222) or port <= 0:
        history_event('register_rejected', level='error', subdomain=subdomain,
                      public_port=port, display_port=target_port,
                      reason='Python broker registrations are SSH/raw-TCP only')
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return
    key = (subdomain, port)
    lock = asyncio.Lock()
    session_conns = set()
    session = (reader, writer, lock, session_conns)
    prev = CTRL_BY_KEY.get(key)
    if prev:
        print(f'WARNING: replacing existing tunnel registration for key={key!r}', file=sys.stderr, flush=True)
    CTRL_BY_KEY[key] = session
    display_port = int(hello.get('display_port') or port)
    url = f'ssh://{subdomain}.{DOMAIN}:{port}'
    META_BY_KEY[key] = {
        'started_at': utc_now(),
        'expires_at': str(hello.get('expires_at') or '').strip(),
        'last_ping_at': '',
        'display_port': display_port,
        'run_id': str(hello.get('run_id') or '').strip(),
        'target': hello.get('target') or '',
        'active_connections': 0,
        'total_connections': 0,
        'peak_connections': 0,
    }
    history_event('register', subdomain=subdomain, public_port=port, display_port=display_port,
                  run_id=META_BY_KEY[key]['run_id'], target=META_BY_KEY[key]['target'], expires_at=META_BY_KEY[key]['expires_at'])
    await send(writer, {'type': 'registered', 'url': url, 'subdomain': subdomain, 'port': port}, lock)
    # The control socket carries relayed payload for every connection on this
    # registration; without a bigger transport buffer the broker pays one RTT
    # per frame and the whole tunnel becomes stop-and-wait.
    raise_buffer_limits(writer)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(recv(reader), timeout=45)
            except asyncio.TimeoutError:
                break
            except (ValueError, UnicodeDecodeError):
                break
            if msg is None:
                break
            if msg.get('type') == 'ping':
                if key in META_BY_KEY:
                    META_BY_KEY[key]['last_ping_at'] = utc_now()
                history_event('control_ping', subdomain=subdomain, public_port=port)
                await send(writer, {'type': 'pong'}, lock)
                continue
            entry = REG.get(('conn', msg.get('id')))
            if entry:
                to_pub, state = entry
                if state['dead']:
                    continue
                if msg.get('type') == 'data' and not state['flow_paused'] and to_pub.qsize() >= FLOW_PAUSE_AT:
                    state['flow_paused'] = True
                    await send(writer, {'type': 'flow', 'id': msg.get('id'), 'paused': True}, lock)
                try:
                    to_pub.put_nowait(msg)
                except asyncio.QueueFull:
                    # Pause was sent with half the queue still available, so
                    # this is only the bounded in-flight tail. Preserve data
                    # while TCP and the per-connection flow signal push
                    # backpressure to the local producer.
                    await to_pub.put(msg)
    finally:
        history_event('disconnect', level='error', subdomain=subdomain, public_port=port, reason='control session ended')
        if CTRL_BY_KEY.get(key) is session:
            CTRL_BY_KEY.pop(key, None)
            META_BY_KEY.pop(key, None)
        for conn_id in list(session_conns):
            entry = REG.get(('conn', conn_id))
            if entry:
                try:
                    entry[0].put_nowait({'type': 'close'})
                except asyncio.QueueFull:
                    pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def handle_public_local(reader, writer):
    """Localhost-only broker HTTP endpoint used by Caddy and host diagnostics."""
    sock = writer.get_extra_info('socket')
    _ip, port = original_dst(sock) if sock else ('', 0)
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=2)
    except Exception:
        initial = b''
    host = parse_host(initial)
    _method, path = parse_request(initial)
    if await serve_broker_if_allowed(writer, initial, host, path, port):
        return
    await write_no_tunnel(writer, initial, host, port)


async def serve_broker_if_allowed(writer, initial, host, path, port):
    allowed_paths = {'', '/', '/api/status', '/api/registrations', '/api/history', '/api/events'}
    if host == BROKER_HOST and path in allowed_paths:
        loopback = peer_is_loopback(writer)
        caddy_basic_ok = loopback and request_header(initial, 'X-Agentsweb-Broker-Auth') == '1'
        public_status_ok = loopback and path == '/api/status'
        token_ok = bool(BROKER_API_TOKEN) and bearer_token(initial) == BROKER_API_TOKEN
        if caddy_basic_ok or public_status_ok or token_ok:
            await write_broker_response(writer, path or '/')
        else:
            body = '{"error":"Unauthorized"}\n'
            writer.write(http_response(401, 'application/json; charset=utf-8', body))
            await writer.drain()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return True
    return False


async def handle_public(reader, writer):
    sock = writer.get_extra_info('socket')
    _ip, port = original_dst(sock) if sock else ('', 0)
    try:
        initial = await asyncio.wait_for(reader.read(4096), timeout=2)
    except Exception:
        initial = b''
    host = parse_host(initial)
    _method, path = parse_request(initial)
    if _method:
        # The Python broker is raw SSH/TCP only. HTTP services use the FRP
        # encoded-hostname path and must never be reachable on public ports.
        await write_bytes(writer, b'')
        return
    if await serve_broker_if_allowed(writer, initial, host, path, port):
        return
    matches = [session for (_sub, public_port), session in CTRL_BY_KEY.items() if public_port == port]
    ctrl = matches[0] if len(matches) == 1 else None

    if not ctrl:
        # Public high ports are retained for explicitly registered raw tunnels
        # such as SSH. An unmatched port is not an HTTP endpoint: close it
        # without emitting the broker's legacy 502 page.
        await write_bytes(writer, b'')
        return

    control_reader, control_writer, lock, session_conns = ctrl
    routed_key = None
    for meta_key, session in CTRL_BY_KEY.items():
        if session is ctrl:
            routed_key = meta_key
            break
    if routed_key in META_BY_KEY:
        active = int(META_BY_KEY[routed_key].get('active_connections') or 0)
        if active >= MAX_CONNS_PER_REGISTRATION:
            await write_busy(writer, initial, host, port, active)
            return
        META_BY_KEY[routed_key]['active_connections'] = active + 1
        META_BY_KEY[routed_key]['total_connections'] = int(META_BY_KEY[routed_key].get('total_connections') or 0) + 1
        peak = int(META_BY_KEY[routed_key].get('peak_connections') or 0)
        if active + 1 > peak:
            META_BY_KEY[routed_key]['peak_connections'] = active + 1
    conn_id = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    to_client = asyncio.Queue(maxsize=CONN_QUEUE_MAX)
    to_pub = asyncio.Queue(maxsize=CONN_QUEUE_MAX)
    state = {'dead': False, 'flow_paused': False}
    REG[('conn', conn_id)] = (to_pub, state)
    session_conns.add(conn_id)
    try:
        await send(control_writer, {'type': 'open', 'id': conn_id, 'port': port, 'host': '', 'initial': b64(initial)}, lock)
    except Exception:
        session_conns.discard(conn_id)
        REG.pop(('conn', conn_id), None)
        if routed_key in META_BY_KEY:
            META_BY_KEY[routed_key]['active_connections'] = max(0, int(META_BY_KEY[routed_key].get('active_connections') or 0) - 1)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return

    async def queue_to_client():
        """Drains the bounded to_client queue onto the shared control socket.
        Blocking here only ever stalls this one connection: if the client stops
        reading, to_client fills, pub_to_client pauses and times out, and this
        connection is dropped instead of stalling the other ports."""
        try:
            while True:
                msg = await to_client.get()
                if msg is None:
                    break
                if msg.get('type') == 'data':
                    await send(control_writer, {'type': 'data', 'id': conn_id, 'data': msg['data']}, lock)
                else:
                    await send(control_writer, {'type': 'close', 'id': conn_id, 'error': msg.get('error', '')}, lock)
                    break
        except Exception:
            pass

    async def pub_to_client():
        paused = False
        pending = b''
        full_since = None
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not pending:
                    data = await reader.read(32768)
                    if not data:
                        break
                    pending = data
                try:
                    to_client.put_nowait({'type': 'data', 'data': b64(pending)})
                    pending = b''
                    if paused:
                        paused = False
                        reader.resume_reading()
                except asyncio.QueueFull:
                    if not paused:
                        paused = True
                        full_since = loop.time()
                        reader.pause_reading()
                    elif full_since is not None and loop.time() - full_since >= CONN_STALL_TIMEOUT:
                        break
                    await asyncio.sleep(0.05)
        finally:
            try:
                to_client.put_nowait({'type': 'close'})
            except asyncio.QueueFull:
                pass

    async def client_to_pub():
        wrote_data = False
        raise_buffer_limits(writer)
        total = 0
        batches = 0
        reason = 'worker close frame'
        try:
            while True:
                if state['dead']:
                    reason = 'connection marked dead'
                    break
                try:
                    msg = await asyncio.wait_for(to_pub.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                msgs = [msg]
                try:
                    while len(msgs) < CONN_QUEUE_MAX:
                        msgs.append(to_pub.get_nowait())
                except asyncio.QueueEmpty:
                    pass
                close_msg = None
                for m in msgs:
                    if m.get('type') == 'close':
                        close_msg = m
                        break
                    data = ub64(m['data'])
                    if data:
                        wrote_data = True
                    writer.write(data)
                    total += len(data)
                if close_msg is not None:
                    if not wrote_data and close_msg.get('error') and parse_request(initial)[0]:
                        await write_target_error(writer, initial, host, port, close_msg.get('error', ''))
                    reason = 'close frame'
                    break
                batches += 1
                await writer.drain()
                if state['flow_paused'] and to_pub.qsize() <= FLOW_RESUME_AT:
                    state['flow_paused'] = False
                    await send(control_writer, {'type': 'flow', 'id': conn_id, 'paused': False}, lock)
        finally:
            print(f'conn {conn_id} public close reason={reason} bytes={total} batches={batches} queued={to_pub.qsize()}', file=sys.stderr, flush=True)
            writer.close()

    t1 = asyncio.create_task(pub_to_client())
    t2 = asyncio.create_task(client_to_pub())
    t3 = asyncio.create_task(queue_to_client())
    await asyncio.wait({t1, t2, t3}, return_when=asyncio.FIRST_COMPLETED)
    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    REG.pop(('conn', conn_id), None)
    state['dead'] = True
    session_conns.discard(conn_id)
    if routed_key in META_BY_KEY:
        META_BY_KEY[routed_key]['active_connections'] = max(0, int(META_BY_KEY[routed_key].get('active_connections') or 0) - 1)
    if not t3.done():
        t3.cancel()
        try:
            await t3
        except asyncio.CancelledError:
            pass
    try:
        await send(control_writer, {'type': 'close', 'id': conn_id}, lock)
    except Exception:
        pass
    try:
        await writer.wait_closed()
    except Exception:
        pass


def error_page(status, reason, headline, lines, refresh_seconds):
    lines_text = ''.join(html.escape(line) + '\n' for line in lines)
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agentsweb tunnel broker</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #111827;
      color: #e5e7eb;
    }}
    main {{
      max-width: 720px;
      padding: 24px;
      margin: 24px;
      border: 1px solid #374151;
      border-radius: 12px;
      background: #0b1220;
      box-shadow: 0 10px 30px rgba(0,0,0,0.35);
    }}
    h1 {{ margin-top: 0; font-size: 1.2rem; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      padding: 16px;
      border-radius: 8px;
      background: #111827;
      border: 1px solid #1f2937;
    }}
  </style>
</head>
<body>
  <main>
    <h1>agentsweb tunnel broker</h1>
    <pre>{html.escape(headline)}

{lines_text}</pre>
  </main>
</body>
</html>
""".encode()
    headers = (
        f'HTTP/1.1 {status} {reason}\r\n'.encode()
        + b'content-type: text/html; charset=utf-8\r\n'
        + b'content-length: ' + str(len(body)).encode() + b'\r\n'
        + b'cache-control: no-store, max-age=0\r\n\r\n'
    )
    return headers + body


async def write_bytes(writer, payload):
    if not payload:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return
    try:
        writer.write(payload)
        await writer.drain()
    except Exception:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def write_no_tunnel(writer, initial, host, port):
    host_text = host or 'None'
    payload = error_page(
        502,
        'Bad Gateway',
        f'no active tunnel for host={host_text} port={port}',
        [],
        3,
    )
    await write_bytes(writer, payload)


async def write_target_error(writer, initial, host, port, error):
    host_text = host or 'None'
    port_text = port or 'any'
    payload = error_page(
        502,
        'Bad Gateway',
        f'tunnel for host={host_text} port={port_text} is up, but the runner could not reach its local target port.',
        [
            error or 'unknown error',
            'The agent on the runner may still be starting, or the port may be closed. Retrying in 5s.',
        ],
        5,
    )
    await write_bytes(writer, payload)


async def write_busy(writer, initial, host, port, active):
    host_text = host or 'None'
    port_text = port or 'any'
    payload = error_page(
        429,
        'Too Many Requests',
        f'tunnel for host={host_text} port={port_text} is at capacity ({active} active connections).',
        ['Retrying in 3s.'],
        3,
    )
    await write_bytes(writer, payload)


async def server():
    public_server = await asyncio.start_server(handle_public, '0.0.0.0', BROKER_PORT)
    local_registry_server = await asyncio.start_server(handle_public_local, '127.0.0.1', LOCAL_REGISTRY_PORT)
    control_server = await asyncio.start_server(handle_control, '0.0.0.0', CONTROL_PORT)
    print(f'broker public:{BROKER_PORT} local-registry:{LOCAL_REGISTRY_PORT} control:{CONTROL_PORT}', flush=True)
    async with public_server, local_registry_server, control_server:
        probe_task = asyncio.create_task(probe_registrations())
        try:
            await asyncio.gather(public_server.serve_forever(), local_registry_server.serve_forever(), control_server.serve_forever())
        finally:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)


def main():
    argparse.ArgumentParser(description='Run agentsweb tunnel broker').parse_args()
    asyncio.run(server())


if __name__ == '__main__':
    main()
