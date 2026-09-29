"""WSGI transport for existing domain handlers, with no nested HTTP server."""
import importlib.util
from email.message import Message
from http import HTTPStatus
from io import BytesIO
import logging
from pathlib import Path
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pharmacy_web', ROOT / 'import hashlib.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
logger = logging.getLogger('pharmacia')


class WSGIHandler(app.PharmacyHandler):
    def __init__(self, environ):
        self.command = environ['REQUEST_METHOD']
        self.path = environ.get('PATH_INFO', '/')
        if environ.get('QUERY_STRING'):
            self.path += '?' + environ['QUERY_STRING']
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith('HTTP_'):
                self.headers[key[5:].replace('_', '-')] = value
        for key in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
            if environ.get(key):
                self.headers[key.replace('_', '-')] = environ[key]
        self.rfile = environ['wsgi.input']
        self.wfile = BytesIO()
        self.response_status = 200
        self.response_headers = []

    def send_response(self, code, message=None):
        self.response_status = code

    def send_header(self, name, value):
        self.response_headers.append((name, str(value)))

    def end_headers(self):
        existing = {name.lower() for name, _ in self.response_headers}
        headers = {'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': 'same-origin',
                   'Content-Security-Policy': "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"}
        if app.settings.production():
            headers['Strict-Transport-Security'] = 'max-age=31536000'
        self.response_headers.extend((k, v) for k, v in headers.items() if k.lower() not in existing)


def application(environ, start_response):
    started = time.monotonic()
    handler = WSGIHandler(environ)
    try:
        configured = app.settings.public_origin()
        if configured and handler.headers.get('Host', '').lower() != urlsplit(configured).netloc.lower():
            # Health probes need no domain or application data.
            if environ.get('PATH_INFO') != '/health':
                raise app.ApiError(400, 'Domínio não autorizado.')
        method = handler.command
        if method not in ('GET', 'POST', 'PUT', 'DELETE', 'HEAD'):
            raise app.ApiError(405, 'Método não permitido.')
        getattr(handler, 'do_GET' if method == 'HEAD' else 'do_' + method)()
    except app.ApiError as error:
        handler.send_error_json(error)
    except Exception as error:
        logger.error('transport_failed error_type=%s', type(error).__name__)
        handler = WSGIHandler(environ)
        handler.send_json(500, {'error': 'Não foi possível concluir a solicitação. Tente novamente.'})
    # Never log URL/query, headers, bodies, username or exception text.
    logger.info('request method=%s status=%s duration_ms=%d', handler.command,
                handler.response_status, int((time.monotonic() - started) * 1000))
    start_response(f'{handler.response_status} {HTTPStatus(handler.response_status).phrase}', handler.response_headers)
    return [b'' if handler.command == 'HEAD' else handler.wfile.getvalue()]
