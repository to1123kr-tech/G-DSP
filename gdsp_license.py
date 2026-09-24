# -*- coding: utf-8 -*-
"""
gdsp_license.py — 락키 발급·확인 (판매판용)

server.py 에 아래 두 줄로 붙는다. 문의 게시판(gdsp_inquiry.py)과 같은 방식이다.

    from gdsp_license import register_license
    register_license(app)

무엇을 하나
    · 고객 프로그램이 "이 락키 아직 살아 있어요?" 하고 물으면 답한다
    · 영대님이 락키를 발급·해지·연장한다 (관리자 비밀번호 필요)

왜 서버가 필요한가  ★
    락키가 고객 PC 에만 있으면 **환불해도 계속 쓴다.**
    서버에 목록을 두면 지우는 순간 멈춘다. 이게 이 파일의 존재 이유다.

무엇을 담지 않나
    · 비밀번호는 이 파일에 없다 (gdsp_admin_pw.txt / 환경변수)
    · 서명용 비밀값도 없다 (gdsp_lic_secret.txt) — 이 저장소는 공개다

보관 파일 (둘 다 .gitignore 에 넣을 것)
    gdsp_licenses.json     발급한 락키 목록
    gdsp_lic_secret.txt    응답 서명용 비밀값 (없으면 처음 켤 때 만든다)
"""

import os
import json
import hmac
import hashlib
import secrets
import threading
import logging
from datetime import datetime, timedelta

from flask import request, jsonify

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(_DIR, 'gdsp_licenses.json')
_PW_FILE = os.path.join(_DIR, 'gdsp_admin_pw.txt')
_SECRET_FILE = os.path.join(_DIR, 'gdsp_lic_secret.txt')
_LOCK = threading.Lock()

# 락키에 쓰는 글자 — 헷갈리는 것은 뺐다.
#   0 과 O, 1 과 I·L 은 전화로 불러줄 때 반드시 틀린다.
_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'

MAX_DEVICE_LEN = 64
MAX_TEXT_LEN = 200


# ──────────────────────────────────────────────────────────────
# 비밀값 · 비밀번호
# ──────────────────────────────────────────────────────────────

def _admin_pw():
    v = (os.environ.get('GDSP_ADMIN_PW') or '').strip()
    if v:
        return v
    try:
        with open(_PW_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _secret():
    """응답에 도장을 찍을 비밀값. 없으면 처음 한 번 만든다."""
    v = (os.environ.get('GDSP_LIC_SECRET') or '').strip()
    if v:
        return v
    try:
        with open(_SECRET_FILE, 'r', encoding='utf-8') as f:
            v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    v = secrets.token_hex(32)
    try:
        with open(_SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(v)
        os.chmod(_SECRET_FILE, 0o600)
    except Exception as e:
        logger.error('[LIC] 비밀값을 저장하지 못했습니다: %s', e)
    return v


def _is_admin():
    pw = _admin_pw()
    if not pw:
        return False
    given = ''
    body = request.get_json(silent=True) or {}
    given = (body.get('pw') or request.headers.get('X-Admin-Pw') or '').strip()
    return bool(given) and hmac.compare_digest(given, pw)


# ──────────────────────────────────────────────────────────────
# 저장 — 방문통계·문의와 같은 방식 (원자적 저장)
# ──────────────────────────────────────────────────────────────

def _load():
    try:
        with open(_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault('keys', {})
    return d


def _save(d):
    tmp = _FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _FILE)          # 쓰다 죽어도 원본이 남는다


# ──────────────────────────────────────────────────────────────
# 거들기
# ──────────────────────────────────────────────────────────────

def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _make_key():
    """GDSP-XXXX-XXXX-XXXX 모양. 전화로 불러줄 수 있게 짧게."""
    part = lambda: ''.join(secrets.choice(_ALPHABET) for _ in range(4))
    return 'GDSP-%s-%s-%s' % (part(), part(), part())


def _norm(key):
    """띄어쓰기·소문자·하이픈 빠짐을 모두 받아준다.

    고객이 카톡에서 복사하면 공백이 섞이고, 손으로 치면 하이픈을 빠뜨린다.
    받아들이는 쪽이 너그러워야 문의가 줄어든다.
    """
    s = ''.join(ch for ch in (key or '').upper()
                if ch.isalnum())
    if s.startswith('GDSP'):
        s = s[4:]
    if len(s) != 12:
        return ''
    return 'GDSP-%s-%s-%s' % (s[0:4], s[4:8], s[8:12])


def _clip(s, n=MAX_TEXT_LEN):
    return (str(s or '')).strip()[:n]


def _stamp(device, expire, features):
    """고객 프로그램이 들고 있을 답에 도장을 찍는다.

    인터넷이 끊겨도 며칠은 이 답을 그대로 쓴다. 도장이 있어야 그 사이에
    손으로 고쳐 기간을 늘리는 것을 막는다.
    (완벽하지는 않다 — 진짜 방어선은 '첫 실행 때 서버 확인' 이다)
    """
    msg = '%s|%s|%s' % (device, expire, ','.join(sorted(features)))
    return hmac.new(_secret().encode('utf-8'),
                    msg.encode('utf-8'), hashlib.sha256).hexdigest()


# ──────────────────────────────────────────────────────────────
# 붙이기
# ──────────────────────────────────────────────────────────────

def register_license(app):

    # ── 고객 프로그램이 부르는 곳 ──────────────────────────
    @app.route('/api/lic/check', methods=['POST'])
    def lic_check():
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        device = _clip(body.get('device'), MAX_DEVICE_LEN)

        if not key:
            return jsonify(ok=False, reason='badkey',
                           message='락키 모양이 올바르지 않습니다.')
        if not device:
            return jsonify(ok=False, reason='nodevice',
                           message='장치번호가 없습니다.')

        with _LOCK:
            d = _load()
            row = d['keys'].get(key)
            if not row:
                return jsonify(ok=False, reason='unknown',
                               message='등록되지 않은 락키입니다.')

            if not row.get('active', True):
                return jsonify(ok=False, reason='revoked',
                               message='해지된 락키입니다. 구매처에 문의해 주세요.')

            # 처음 확인하는 PC 면 여기에 묶는다. 다음부터 그 PC 에서만 열린다.
            if not row.get('device'):
                row['device'] = device
            elif row['device'] != device:
                return jsonify(ok=False, reason='device',
                               message='다른 컴퓨터에 등록된 락키입니다. '
                                       '컴퓨터를 바꾸셨으면 구매처에 문의해 주세요.')

            expire = row.get('expire') or ''
            if expire and _today() > expire:
                return jsonify(ok=False, reason='expired', expire=expire,
                               message='사용 기간이 끝났습니다 (%s).' % expire)

            row['last_seen'] = _today()
            row['seen'] = int(row.get('seen', 0)) + 1
            _save(d)

        features = list(row.get('features') or [])
        return jsonify(ok=True, features=features, expire=expire,
                       buyer=row.get('buyer', ''),
                       stamp=_stamp(device, expire, features),
                       today=_today())

    # ── 관리자 ────────────────────────────────────────────
    @app.route('/api/lic/issue', methods=['POST'])
    def lic_issue():
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}

        months = int(body.get('months') or 12)
        features = [f for f in (body.get('features') or ['vworld'])
                    if isinstance(f, str)][:20]
        expire = (datetime.now() + timedelta(days=int(months * 30.44))
                  ).strftime('%Y-%m-%d')

        with _LOCK:
            d = _load()
            for _ in range(50):                  # 겹치면 다시 뽑는다
                key = _make_key()
                if key not in d['keys']:
                    break
            else:
                return jsonify(ok=False, message='락키를 만들지 못했습니다.'), 500
            d['keys'][key] = {
                'buyer': _clip(body.get('buyer')),
                'note': _clip(body.get('note')),
                'features': features,
                'expire': expire,
                'issued': _today(),
                'device': _clip(body.get('device'), MAX_DEVICE_LEN),
                'active': True,
                'last_seen': '',
                'seen': 0,
            }
            _save(d)
        return jsonify(ok=True, key=key, expire=expire, features=features)

    @app.route('/api/lic/revoke', methods=['POST'])
    def lic_revoke():
        """환불했을 때. 다음 확인에서 멈춘다."""
        return _flip(True)

    @app.route('/api/lic/restore', methods=['POST'])
    def lic_restore():
        """잘못 해지했을 때 되돌리기."""
        return _flip(False)

    def _flip(revoke):
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        with _LOCK:
            d = _load()
            row = d['keys'].get(key)
            if not row:
                return jsonify(ok=False, message='없는 락키입니다.'), 404
            row['active'] = (not revoke)
            row['note'] = _clip(body.get('note') or row.get('note'))
            _save(d)
        return jsonify(ok=True, key=key, active=row['active'])

    @app.route('/api/lic/renew', methods=['POST'])
    def lic_renew():
        """기간 연장. 아직 남아 있으면 그 뒤에 이어 붙인다."""
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        months = int(body.get('months') or 12)
        with _LOCK:
            d = _load()
            row = d['keys'].get(key)
            if not row:
                return jsonify(ok=False, message='없는 락키입니다.'), 404
            base = row.get('expire') or _today()
            if base < _today():
                base = _today()          # 이미 지났으면 오늘부터
            start = datetime.strptime(base, '%Y-%m-%d')
            row['expire'] = (start + timedelta(days=int(months * 30.44))
                             ).strftime('%Y-%m-%d')
            row['active'] = True
            _save(d)
        return jsonify(ok=True, key=key, expire=row['expire'])

    @app.route('/api/lic/reset-device', methods=['POST'])
    def lic_reset_device():
        """컴퓨터를 바꿨을 때. 다음 실행하는 PC 에 다시 묶인다."""
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        with _LOCK:
            d = _load()
            row = d['keys'].get(key)
            if not row:
                return jsonify(ok=False, message='없는 락키입니다.'), 404
            row['device'] = ''
            _save(d)
        return jsonify(ok=True, key=key)

    @app.route('/api/lic/features', methods=['POST'])
    def lic_features():
        """산 메뉴 바꾸기 (추가 구매)."""
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        feats = [f for f in (body.get('features') or []) if isinstance(f, str)][:20]
        with _LOCK:
            d = _load()
            row = d['keys'].get(key)
            if not row:
                return jsonify(ok=False, message='없는 락키입니다.'), 404
            row['features'] = feats
            _save(d)
        return jsonify(ok=True, key=key, features=feats)

    @app.route('/api/lic/delete', methods=['POST'])
    def lic_delete():
        """목록에서 아주 지운다 (잘못 발급했을 때).

        환불은 '해지'(revoke)를 쓴다. 지워 버리면 누가 언제 샀는지도
        같이 사라져 나중에 문의가 왔을 때 확인할 수 없다.
        """
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        body = request.get_json(silent=True) or {}
        key = _norm(body.get('key'))
        with _LOCK:
            d = _load()
            if key not in d['keys']:
                return jsonify(ok=False, message='없는 락키입니다.'), 404
            d['keys'].pop(key)
            _save(d)
        return jsonify(ok=True, key=key)

    @app.route('/api/lic/list', methods=['POST'])
    def lic_list():
        if not _is_admin():
            return jsonify(ok=False, message='관리자만 할 수 있습니다.'), 403
        with _LOCK:
            d = _load()
        rows = []
        for key, r in sorted(d['keys'].items(),
                             key=lambda kv: kv[1].get('issued', ''),
                             reverse=True):
            rows.append(dict(r, key=key,
                             left=_days_left(r.get('expire'))))
        return jsonify(ok=True, count=len(rows), keys=rows)

    logger.info('[LIC] 락키 창구를 붙였습니다.')
    return app


def _days_left(expire):
    if not expire:
        return None
    try:
        d = datetime.strptime(expire, '%Y-%m-%d') - datetime.now()
        return d.days
    except Exception:
        return None
