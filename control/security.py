import hashlib
import hmac
import json
import os
import re
import time


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def normalize_user(value):
    value = str(value or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', value):
        raise ValueError('用户名格式无效')
    return value


def users():
    raw = os.getenv('CLOUDOPS_USERS', '')
    if raw:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value:
            raise ValueError('CLOUDOPS_USERS 必须是非空 JSON 对象')
        return {normalize_user(k): str(v) for k, v in value.items()}
    return {'admin': os.environ.get('ADMIN_PASSWORD', '')}


def authenticate(user_id, password):
    try:
        user_id = normalize_user(user_id or 'admin')
    except ValueError:
        return None
    expected = users().get(user_id)
    return user_id if expected and hmac.compare_digest(str(password), expected) else None


def session_token(user_id='admin'):
    user_id = normalize_user(user_id)
    expires = str(int(time.time()) + 12 * 3600)
    sig = hmac.new(os.environ['SESSION_SECRET'].encode(), (user_id + ':' + expires).encode(), hashlib.sha256).hexdigest()
    return user_id + '.' + expires + '.' + sig


def session_user(token):
    try:
        user_id, expires, sig = token.split('.', 2)
        user_id = normalize_user(user_id)
        expected = hmac.new(os.environ['SESSION_SECRET'].encode(), (user_id + ':' + expires).encode(), hashlib.sha256).hexdigest()
        return user_id if int(expires) > time.time() and hmac.compare_digest(sig, expected) else None
    except (ValueError, KeyError, AttributeError):
        return None


def valid_session(token):
    return session_user(token) is not None


def approved(a, action):
    return a and a.get('status') == 'approved' and a.get('expires_at', 0) > time.time() and a.get('fingerprint') == fingerprint(action)


def validate_auth_config():
    if len(os.getenv('SESSION_SECRET','')) < 32:
        raise RuntimeError('SESSION_SECRET must contain at least 32 characters')
    if any(len(p) < 12 for p in users().values()):
        raise RuntimeError('Configure application passwords of at least 12 characters')
