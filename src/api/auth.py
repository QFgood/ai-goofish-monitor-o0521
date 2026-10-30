"""Server-validated HttpOnly sessions; credentials never live in browser storage."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from importlib import import_module
config = import_module('src.infrastructure.config.settings')
from src.infrastructure.persistence.sqlite_connection import get_database_path

COOKIE = 'goofish_session'
TTL = 12 * 3600
_attempts = {}


def auth_enabled():
    return os.getenv('WEB_AUTH_ENABLED', 'true').strip().lower() not in {'false', '0', 'no', 'off'}


def credentials_ready():
    password = config.settings.web_password
    return bool(config.settings.web_username.strip() and len(password) >= 12 and password not in {'admin123', 'changeme123456'})


def session_key():
    configured = os.getenv('WEB_SESSION_SECRET')
    if configured:
        if len(configured) < 32:
            raise HTTPException(503, 'WEB_SESSION_SECRET must have at least 32 characters')
        return configured.encode()
    path = Path(get_database_path()).parent / '.session_secret'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise HTTPException(503, 'Session key file requires mode 600')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(48))
    key = path.read_bytes()
    if len(key) < 32:
        raise HTTPException(503, 'Invalid session key file')
    return key


def signature(payload):
    # Rotating application credentials invalidates all previous sessions.
    credential_key = hmac.new(session_key(), (config.settings.web_username + '\0' + config.settings.web_password).encode(), hashlib.sha256).digest()
    return hmac.new(credential_key, payload.encode(), hashlib.sha256).hexdigest()


def issue_session(username):
    payload = base64.urlsafe_b64encode(json.dumps({'user': username, 'exp': int(time.time()) + TTL,
                                                'nonce': secrets.token_hex(16)}).encode()).decode()
    return payload + '.' + signature(payload)


def session_user(connection):
    if not auth_enabled():
        return '家庭局域网'
    if not credentials_ready():
        return None
    token = connection.cookies.get(COOKIE, '')
    try:
        payload, supplied = token.rsplit('.', 1)
        if len(token) > 2048 or not hmac.compare_digest(supplied, signature(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data['exp'] <= time.time() or data['user'] != config.settings.web_username:
            return None
        return data['user']
    except (ValueError, KeyError, TypeError):
        return None


def same_origin(connection):
    origin = connection.headers.get('origin')
    if not origin:
        return connection.headers.get('sec-fetch-site') not in {'cross-site'}
    parsed = urlsplit(origin)
    return parsed.scheme in {'http', 'https'} and parsed.netloc == connection.headers.get('host')


def require_user(request: Request):
    user = session_user(request)
    if user is None:
        raise HTTPException(401, '请登录，或设置至少12位的非默认应用密码')
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not same_origin(request):
        raise HTTPException(403, '跨站写入已拒绝')
    return user


def check_login_rate(request):
    key = request.client.host if request.client else 'local'
    current = time.monotonic()
    # Expired entries are removed and total memory is bounded.
    for old in list(_attempts):
        if _attempts[old][0] < current - 60:
            del _attempts[old]
    if key not in _attempts and len(_attempts) >= 4096:
        raise HTTPException(429, '登录请求过多，请稍后重试')
    started, count = _attempts.get(key, (current, 0))
    if count >= 8:
        raise HTTPException(429, '登录请求过多，请稍后重试')
    _attempts[key] = (started, count + 1)


class APIAuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == '/api' or request.url.path.startswith('/api/'):
            try:
                require_user(request)
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        if request.url.path.startswith(('/api/', '/auth/')):
            response.headers['Cache-Control'] = 'no-store'
        return response
