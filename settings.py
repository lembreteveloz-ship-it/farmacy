"""Environment configuration; never prints configuration values or credentials."""
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def load_environment():
    # Explicit local file only; process environment always wins.
    from dotenv import load_dotenv
    load_dotenv(ROOT.parent / '.env', override=False)
    for name in ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'SUPERADMIN_USERNAME', 'SUPERADMIN_PASSWORD',
                 'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_FROM', 'SMTP_TLS', 'SECURE_COOKIE'):
        if name in os.environ:
            os.environ['FARMACIA_' + name] = os.environ[name]


def production():
    return os.environ.get('APP_ENV', 'development') == 'production'


def secure_cookie():
    return os.environ.get('SECURE_COOKIE', os.environ.get('FARMACIA_SECURE_COOKIE', '1' if production() else '0')) == '1'


def public_origin():
    return os.environ.get('PUBLIC_ORIGIN', '').rstrip('/')


def validate():
    origin = urlsplit(public_origin())
    if public_origin() and (origin.scheme not in ('http', 'https') or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username):
        raise ValueError('PUBLIC_ORIGIN deve conter somente protocolo e domínio, sem caminho ou credenciais.')
    if production() and (origin.scheme != 'https' or not secure_cookie()):
        raise ValueError('Produção exige PUBLIC_ORIGIN HTTPS e SECURE_COOKIE=1.')
    if os.environ.get('TRUSTED_PROXY') == '*':
        raise ValueError('Informe o endereço específico do proxy, não um curinga.')


def data_directory():
    return Path(os.environ.get('DATA_DIR') or str(ROOT / 'data')).resolve()


def sqlite_path():
    value = os.environ.get('DATABASE_URL', '')
    if not value:
        return data_directory() / 'farmacia.sqlite3'
    if value.startswith(('postgresql://', 'postgres://')):
        return None
    if not value.startswith('sqlite:///'):
        raise ValueError('DATABASE_URL deve usar sqlite:/// ou postgresql://.')
    return Path(value[len('sqlite:///'):]).resolve()
