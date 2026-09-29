"""Production entry point. Configure .env or process environment before startup."""
import argparse
import logging
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor_python'))
import settings


def main():
    settings.load_environment()
    settings.validate()
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    from wsgi import app, application
    parser = argparse.ArgumentParser()
    parser.add_argument('--init-db', action='store_true', help='Initialize/migrate and exit')
    args = parser.parse_args()
    app.initialize_db()
    if args.init_db:
        print('Banco inicializado; nenhuma senha existente foi redefinida.')
        return
    if app.DATABASE is not None:
        threading.Thread(target=app.automatic_backup, daemon=True).start()
    from waitress import serve
    options = dict(host=os.environ.get('HOST') or '0.0.0.0', port=int(os.environ.get('PORT', '8000')),
                   threads=4, max_request_body_size=1_000_000, channel_timeout=60,
                   clear_untrusted_proxy_headers=True, expose_tracebacks=False)
    if os.environ.get('TRUSTED_PROXY'):
        options.update(trusted_proxy=os.environ['TRUSTED_PROXY'],
                       trusted_proxy_headers={'x-forwarded-proto'}, trusted_proxy_count=1)
    serve(application, **options)


if __name__ == '__main__':
    main()
