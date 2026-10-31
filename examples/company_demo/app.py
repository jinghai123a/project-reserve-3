#!/usr/bin/env python3
"""Small local task desk demo: stdlib HTTP, SQLite persistence, sibling web assets."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


BODY_LIMIT = 16 * 1024
WEB_ROOT = Path(__file__).resolve().parent / 'web'
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


@contextmanager
def database(path):
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'TaskDeskDemo/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        # Startup stdout is a machine-readable connection handoff.
        pass

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {'error': message or self.responses.get(code, ('request failed',))[0]})

    def read_object(self):
        if self.headers.get('Transfer-Encoding'):
            self.send_error(400, 'chunked request bodies are not supported')
            return None
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'\d+', lengths[0]):
            self.send_error(400, 'one valid Content-Length header is required')
            return None
        if len(lengths[0]) > 8 or int(lengths[0]) > BODY_LIMIT:
            self.send_error(413, 'JSON body exceeds 16 KiB')
            return None
        try:
            payload = json.loads(self.rfile.read(int(lengths[0])))
        except (ValueError, UnicodeError, RecursionError, TimeoutError, OSError):
            self.send_error(400, 'invalid JSON body')
            return None
        if not isinstance(payload, dict):
            self.send_error(400, 'JSON body must be an object')
            return None
        return payload

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            self.respond(200, {'ok': True})
        elif path == '/api/tasks':
            try:
                with database(self.server.db_path) as connection:
                    rows = connection.execute('SELECT id, title, status FROM tasks ORDER BY id').fetchall()
                self.respond(200, {'tasks': [dict(row) for row in rows]})
            except sqlite3.Error:
                self.send_error(500, 'database operation failed')
        elif path in ASSETS:
            name, content_type = ASSETS[path]
            try:
                body = (WEB_ROOT / name).read_bytes()
            except OSError:
                self.send_error(404, 'asset not found')
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, 'route not found')

    def do_POST(self):
        if urlsplit(self.path).path != '/api/tasks':
            self.send_error(404, 'route not found')
            return
        payload = self.read_object()
        if payload is None:
            return
        title = payload.get('title')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            self.send_error(400, 'title must contain 1 to 120 characters after trimming')
            return
        try:
            title.encode('utf-8')
        except UnicodeError:
            self.send_error(400, 'title must contain valid Unicode text')
            return
        try:
            with database(self.server.db_path) as connection:
                cursor = connection.execute('INSERT INTO tasks(title, status) VALUES (?, ?)', (title.strip(), 'todo'))
                row = connection.execute('SELECT id, title, status FROM tasks WHERE id = ?', (cursor.lastrowid,)).fetchone()
            self.respond(201, {'task': dict(row)})
        except sqlite3.Error:
            self.send_error(500, 'database operation failed')

    def do_PATCH(self):
        match = re.fullmatch(r'/api/tasks/([1-9][0-9]{0,18})', urlsplit(self.path).path)
        if not match or int(match[1]) > 2**63 - 1:
            self.send_error(404, 'task not found')
            return
        payload = self.read_object()
        if payload is None:
            return
        status = payload.get('status')
        if not isinstance(status, str) or status not in ('todo', 'done'):
            self.send_error(400, 'status must be todo or done')
            return
        try:
            with database(self.server.db_path) as connection:
                cursor = connection.execute('UPDATE tasks SET status = ? WHERE id = ?', (status, int(match[1])))
                row = connection.execute('SELECT id, title, status FROM tasks WHERE id = ?', (int(match[1]),)).fetchone() if cursor.rowcount else None
            if row is None:
                self.send_error(404, 'task not found')
            else:
                self.respond(200, {'task': dict(row)})
        except sqlite3.Error:
            self.send_error(500, 'database operation failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0, help='0 chooses an available port')
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    path = args.db.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','done')))")
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.db_path = path
        address, port = server.server_address[:2]
        print(json.dumps({'address': address, 'host': address, 'port': port}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
