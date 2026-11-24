"""Smoke test: start the real server, exercise the debug endpoints end to end."""
import json
import sys
import threading
import time
import urllib.request
import urllib.error
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, '.')
from saltlrc import diagnostics
from saltlrc.server import Handler

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:%d' % server.server_port
failures = []


def check(name, condition, detail=''):
    print(('PASS ' if condition else 'FAIL ') + name + (' :: ' + str(detail) if detail and not condition else ''))
    if not condition:
        failures.append(name)


def post(route, body, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Debug-Token'] = token
    request = urllib.request.Request(url + route, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'{}')


status, _ = post('/api/debug/auth', {'password': 'nope'})
check('wrong password rejected', status == 403, status)
status, payload = post('/api/debug/auth', {'password': diagnostics.DEFAULT_PASSWORD})
check('right password returns token', status == 200 and payload.get('token') == diagnostics.debug_token(), payload)
token = payload.get('token', '')

check('debug page served', b'debug' in urllib.request.urlopen(url + '/debug.html').read().lower())
library_html = urllib.request.urlopen(url + '/library').read()
check('footer has debug link', b'debugLink' in library_html)
check('client hook present', b'/api/debug/client-error' in library_html)

status, _ = post('/api/debug/state', {})
check('state needs token', status == 401, status)

status, payload = post('/api/debug/client-error', {'message': 'ui blew up', 'source': '/library'})
check('client error accepted without token', status == 200, status)

request = urllib.request.Request(url + '/api/debug/state', headers={'X-Debug-Token': token})
state = json.loads(urllib.request.urlopen(request).read())
check('state lists last action', state['requests'] and state['requests'][-1]['path'] == '/api/debug/client-error',
      state['requests'][-1:])
check('state keeps the client error', any(e['message'].startswith('[前端] ui blew up') for e in state['errors']),
      state['errors'][-1:])
check('state exposes log dir', 'log_dir' in state and 'log_file' in state, state.get('log_dir'))

status, payload = post('/api/debug/bundle', {}, token)
check('bundle exported', status == 200 and payload.get('bytes', 0) > 0, payload)
if status == 200:
    with zipfile.ZipFile(payload['path']) as archive:
        names = archive.namelist()
    check('bundle has environment + log + requests + errors',
          set(names) >= {'environment.json', 'log.txt', 'requests.json', 'errors.json'}, names)

server.shutdown()
print('\n%d failure(s)' % len(failures))
raise SystemExit(1 if failures else 0)
