# -*- coding: utf-8 -*-
"""
G-DSP 문의 게시판 (서버 저장 방식)

server.py 와 같은 폴더에 두고, server.py 에 아래 두 줄만 추가하세요.

    from gdsp_inquiry import register_inquiry
    register_inquiry(app)

데이터는 gdsp_inq.json 에 저장됩니다 (방문통계와 동일한 방식).

────────────────────────────────────────────────────────
누가 무엇을 볼 수 있나

  공개 글        → 누구나
  비공개 글      → 관리자, 그리고 글 비밀번호를 맞게 넣은 사람
  비공개 + 비번없음(구버전 글) → 관리자, 작성한 브라우저

  · 목록을 내려줄 때 서버가 판단한다.
  · 볼 수 없는 글은 content 를 아예 비워서 보낸다.
    → 브라우저 개발자도구를 열어도 볼 데이터가 없다.
  · 글 비밀번호는 PBKDF2 해시로만 저장하고 절대 내려보내지 않는다.
  · 관리자 비밀번호는 이 파일에 없다 (gdsp_admin_pw.txt 참고).
────────────────────────────────────────────────────────
"""
import os
import json
import time
import re
import hashlib
import secrets
import threading
import logging
from datetime import datetime

from flask import request, jsonify

logger = logging.getLogger(__name__)

# ── 저장 위치 ──
_DIR = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(_DIR, 'gdsp_inq.json')
_PW_FILE = os.path.join(_DIR, 'gdsp_admin_pw.txt')
_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════
#  관리자 비밀번호
#
#  이 소스에는 비밀번호를 적지 않는다 (저장소가 공개이므로).
#  아래 두 곳 중 하나에서 읽어온다. 위쪽이 우선.
#
#   1) 환경변수  GDSP_ADMIN_PW
#   2) 서버의    gdsp_admin_pw.txt   ← .gitignore 에 반드시 추가
#
#  둘 다 없으면 관리자 로그인 기능이 꺼진 채로 동작한다.
#  파일을 고치면 서버 재시작 없이 바로 반영된다.
# ══════════════════════════════════════════════════════
def _admin_pw():
    v = (os.environ.get('GDSP_ADMIN_PW') or '').strip()
    if v:
        return v
    try:
        with open(_PW_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


# ── 제한값 ──
MAX_CONTENT = 3000        # 문의 내용 최대 글자수
MAX_NAME = 30             # 이름 최대 글자수
MIN_POST_PW = 4           # 글 비밀번호 최소 길이
MAX_ITEMS = 5000          # 보관 최대 건수 (넘으면 오래된 것부터 삭제)
RATE_POSTS = 5            # 같은 IP 가 10분 안에 쓸 수 있는 글 수
RATE_WINDOW = 600
TOKEN_TTL = 8 * 3600      # 관리자 로그인 유지 시간 (8시간)
LOGIN_FAIL_MAX = 10       # 같은 IP 의 10분간 관리자 로그인 실패 허용 횟수
UNLOCK_FAIL_MAX = 20      # 같은 IP 의 10분간 글 비밀번호 실패 허용 횟수

TYPES = ('기능 건의', '오류 신고', '사용법 문의', '기타')

# ── 메모리 상태 (서버 재시작 시 초기화) ──
_TOKENS = {}      # token -> 만료시각
_POST_LOG = {}    # ip -> [작성시각, ...]
_FAIL_LOG = {}    # ip -> [관리자 로그인 실패시각, ...]
_UNLOCK_LOG = {}  # ip -> [글 비번 실패시각, ...]


# ══════════════════════════════════════════════════════
#  파일 입출력 — 방문통계(_visit_load/_visit_save)와 같은 방식
# ══════════════════════════════════════════════════════
def _load():
    try:
        with open(_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError
        d.setdefault('items', [])
        if not isinstance(d['items'], list):
            d['items'] = []
        return d
    except Exception:
        return {'items': []}


def _save(d):
    try:
        tmp = _FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _FILE)          # 원자적 저장 (쓰다 죽어도 원본 보존)
    except Exception as e:
        logger.warning(f"[INQ] 저장 실패: {e}")


# ══════════════════════════════════════════════════════
#  글 비밀번호 — 해시로만 저장한다
# ══════════════════════════════════════════════════════
def _hash_pw(pw):
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'), 60000)
    return f"pbkdf2${salt}${h.hex()}"


def _check_pw(stored, pw):
    if not stored or not pw:
        return False
    try:
        algo, salt, want = stored.split('$', 2)
        if algo != 'pbkdf2':
            return False
        h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'), 60000)
        return secrets.compare_digest(h.hex(), want)
    except Exception:
        return False


# ══════════════════════════════════════════════════════
#  도우미
# ══════════════════════════════════════════════════════
def _ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:45]
    return (request.remote_addr or '?')[:45]


def _clean(s, limit):
    """제어문자 제거 + 길이 제한. HTML 은 화면에서 이스케이프 처리한다."""
    s = str(s or '')
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    return s.strip()[:limit]


def _sweep(log, ip, window):
    """오래된 기록 정리 후 남은 개수 반환"""
    now = time.time()
    arr = [t for t in log.get(ip, []) if now - t < window]
    if arr:
        log[ip] = arr
    else:
        log.pop(ip, None)
    if len(log) > 5000:                 # 메모리 폭주 방지
        log.clear()
    return len(arr)


def _token_of_request():
    body = request.get_json(silent=True) or {}
    return (request.headers.get('X-Admin-Token')
            or body.get('token')
            or request.args.get('token') or '')


def _is_admin():
    tk = _token_of_request()
    if not tk:
        return False
    exp = _TOKENS.get(tk)
    if not exp:
        return False
    if exp < time.time():
        _TOKENS.pop(tk, None)
        return False
    return True


def _can_see(it, admin, uid, unlocked=False):
    """비공개 글의 내용을 보여줄지 판단"""
    if not it.get('secret'):
        return True
    if admin or unlocked:
        return True
    if it.get('pw'):                    # 비번이 걸린 글은 반드시 해제해야 함
        return False
    return bool(uid) and it.get('owner') == uid   # 구버전 글 (비번 없음)


def _can_edit(it, admin, uid, pw):
    """수정·삭제 권한 판단"""
    if admin:
        return True
    if it.get('pw'):
        return _check_pw(it['pw'], pw)
    return bool(uid) and it.get('owner') == uid


def _view(it, admin, uid, unlocked=False):
    """화면에 내려보낼 형태로 변환. 비밀번호 해시는 절대 포함하지 않는다."""
    mine = bool(uid) and it.get('owner') == uid
    can = _can_see(it, admin, uid, unlocked)
    out = {
        'id': it.get('id'),
        'name': it.get('name') or '익명',
        'type': it.get('type') or '기타',
        'secret': bool(it.get('secret')),
        'haspw': bool(it.get('pw')),
        'ts': it.get('ts') or 0,
        'edited': it.get('edited') or 0,
        'mine': mine,
        'locked': not can,
        'content': it.get('content', '') if can else '',
        'reply': it.get('reply') if can else None,
        'answered': bool(it.get('reply')),
    }
    if admin:
        out['ip'] = it.get('ip', '')
    return out


def _find(items, iid):
    for i, it in enumerate(items):
        if it.get('id') == iid:
            return i
    return -1


# ══════════════════════════════════════════════════════
#  라우트 등록
# ══════════════════════════════════════════════════════
def register_inquiry(app):

    @app.route('/api/inq/list', methods=['GET', 'OPTIONS'])
    def inq_list():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        admin = _is_admin()
        uid = _clean(request.args.get('uid'), 64)
        with _LOCK:
            items = _load()['items']
        return jsonify({
            'ok': True,
            'admin': admin,
            'total': len(items),
            'items': [_view(it, admin, uid) for it in items],
        })

    @app.route('/api/inq/add', methods=['POST', 'OPTIONS'])
    def inq_add():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        b = request.get_json(silent=True) or {}
        content = _clean(b.get('content'), MAX_CONTENT)
        if not content:
            return jsonify({'ok': False, 'msg': '내용을 입력하세요'}), 400

        secret = bool(b.get('secret'))
        pw = str(b.get('pw') or '')
        if secret and len(pw) < MIN_POST_PW:
            return jsonify({'ok': False,
                            'msg': f'비공개 문의는 비밀번호가 필요합니다 ({MIN_POST_PW}자 이상)'}), 400
        if pw and len(pw) < MIN_POST_PW:
            return jsonify({'ok': False,
                            'msg': f'비밀번호는 {MIN_POST_PW}자 이상이어야 합니다'}), 400

        ip = _ip()
        if _sweep(_POST_LOG, ip, RATE_WINDOW) >= RATE_POSTS:
            return jsonify({'ok': False, 'msg': '잠시 후 다시 등록해 주세요'}), 429

        typ = b.get('type')
        if typ not in TYPES:
            typ = '기타'

        item = {
            'id': datetime.now().strftime('%y%m%d%H%M%S') + secrets.token_hex(3),
            'name': _clean(b.get('name'), MAX_NAME) or '익명',
            'type': typ,
            'content': content,
            'secret': secret,
            'pw': _hash_pw(pw) if pw else '',
            'owner': _clean(b.get('uid'), 64),
            'ts': int(time.time() * 1000),
            'edited': 0,
            'reply': None,
            'ip': ip,
        }

        with _LOCK:
            d = _load()
            d['items'].insert(0, item)
            if len(d['items']) > MAX_ITEMS:
                d['items'] = d['items'][:MAX_ITEMS]
            _save(d)
        _POST_LOG.setdefault(ip, []).append(time.time())
        logger.info(f"[INQ] 등록 {item['type']} / {item['name']} "
                    f"/ secret={secret} / pw={'있음' if pw else '없음'}")
        return jsonify({'ok': True, 'id': item['id']})

    @app.route('/api/inq/unlock', methods=['POST', 'OPTIONS'])
    def inq_unlock():
        """글 비밀번호를 확인하고 내용을 돌려준다"""
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        ip = _ip()
        if _sweep(_UNLOCK_LOG, ip, RATE_WINDOW) >= UNLOCK_FAIL_MAX:
            return jsonify({'ok': False, 'msg': '시도 횟수 초과. 잠시 후 다시 시도하세요'}), 429

        b = request.get_json(silent=True) or {}
        iid = _clean(b.get('id'), 40)
        pw = str(b.get('pw') or '')
        uid = _clean(b.get('uid'), 64)
        admin = _is_admin()

        with _LOCK:
            d = _load()
            i = _find(d['items'], iid)
            if i < 0:
                return jsonify({'ok': False, 'msg': '문의를 찾을 수 없습니다'}), 404
            it = d['items'][i]

        if not admin and it.get('pw') and not _check_pw(it['pw'], pw):
            _UNLOCK_LOG.setdefault(ip, []).append(time.time())
            return jsonify({'ok': False, 'msg': '비밀번호가 맞지 않습니다'}), 401

        return jsonify({'ok': True, 'item': _view(it, admin, uid, unlocked=True)})

    @app.route('/api/inq/edit', methods=['POST', 'OPTIONS'])
    def inq_edit():
        """작성자가 글 비밀번호로 내용 수정"""
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        b = request.get_json(silent=True) or {}
        iid = _clean(b.get('id'), 40)
        uid = _clean(b.get('uid'), 64)
        pw = str(b.get('pw') or '')
        content = _clean(b.get('content'), MAX_CONTENT)
        admin = _is_admin()
        if not content:
            return jsonify({'ok': False, 'msg': '내용을 입력하세요'}), 400

        with _LOCK:
            d = _load()
            i = _find(d['items'], iid)
            if i < 0:
                return jsonify({'ok': False, 'msg': '문의를 찾을 수 없습니다'}), 404
            if not _can_edit(d['items'][i], admin, uid, pw):
                return jsonify({'ok': False, 'msg': '비밀번호가 맞지 않습니다'}), 403
            d['items'][i]['content'] = content
            d['items'][i]['edited'] = int(time.time() * 1000)
            _save(d)
        return jsonify({'ok': True})

    @app.route('/api/inq/del', methods=['POST', 'OPTIONS'])
    def inq_del():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        b = request.get_json(silent=True) or {}
        iid = _clean(b.get('id'), 40)
        uid = _clean(b.get('uid'), 64)
        pw = str(b.get('pw') or '')
        admin = _is_admin()
        with _LOCK:
            d = _load()
            i = _find(d['items'], iid)
            if i < 0:
                return jsonify({'ok': False, 'msg': '이미 삭제된 문의입니다'}), 404
            if not _can_edit(d['items'][i], admin, uid, pw):
                return jsonify({'ok': False, 'msg': '비밀번호가 맞지 않습니다'}), 403
            d['items'].pop(i)
            _save(d)
        return jsonify({'ok': True})

    @app.route('/api/inq/reply', methods=['POST', 'OPTIONS'])
    def inq_reply():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        if not _is_admin():
            return jsonify({'ok': False, 'msg': '관리자만 답변할 수 있습니다'}), 403
        b = request.get_json(silent=True) or {}
        iid = _clean(b.get('id'), 40)
        content = _clean(b.get('content'), MAX_CONTENT)
        with _LOCK:
            d = _load()
            i = _find(d['items'], iid)
            if i < 0:
                return jsonify({'ok': False, 'msg': '문의를 찾을 수 없습니다'}), 404
            d['items'][i]['reply'] = ({'content': content,
                                       'ts': int(time.time() * 1000)}
                                      if content else None)
            _save(d)
        return jsonify({'ok': True})

    @app.route('/api/inq/secret', methods=['POST', 'OPTIONS'])
    def inq_secret():
        """관리자가 공개 ↔ 비공개 전환"""
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        if not _is_admin():
            return jsonify({'ok': False, 'msg': '권한이 없습니다'}), 403
        b = request.get_json(silent=True) or {}
        iid = _clean(b.get('id'), 40)
        with _LOCK:
            d = _load()
            i = _find(d['items'], iid)
            if i < 0:
                return jsonify({'ok': False, 'msg': '문의를 찾을 수 없습니다'}), 404
            d['items'][i]['secret'] = bool(b.get('secret'))
            _save(d)
        return jsonify({'ok': True})

    @app.route('/api/inq/login', methods=['POST', 'OPTIONS'])
    def inq_login():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        ip = _ip()
        if _sweep(_FAIL_LOG, ip, RATE_WINDOW) >= LOGIN_FAIL_MAX:
            return jsonify({'ok': False, 'msg': '시도 횟수 초과. 잠시 후 다시 시도하세요'}), 429

        real = _admin_pw()
        if not real:
            logger.error("[INQ] 관리자 비밀번호가 설정되지 않았습니다 "
                         f"— 환경변수 GDSP_ADMIN_PW 또는 {_PW_FILE}")
            return jsonify({'ok': False,
                            'msg': '관리자 비밀번호가 서버에 설정되지 않았습니다'}), 503

        b = request.get_json(silent=True) or {}
        pw = str(b.get('pw') or '')
        if not secrets.compare_digest(pw, real):
            _FAIL_LOG.setdefault(ip, []).append(time.time())
            logger.warning(f"[INQ] 관리자 로그인 실패 ip={ip}")
            return jsonify({'ok': False, 'msg': '비밀번호가 맞지 않습니다'}), 401

        now = time.time()
        for t in [t for t, e in _TOKENS.items() if e < now]:
            _TOKENS.pop(t, None)

        tk = secrets.token_urlsafe(24)
        _TOKENS[tk] = now + TOKEN_TTL
        _FAIL_LOG.pop(ip, None)
        logger.info(f"[INQ] 관리자 로그인 성공 ip={ip}")
        return jsonify({'ok': True, 'token': tk, 'ttl': TOKEN_TTL})

    @app.route('/api/inq/logout', methods=['POST', 'OPTIONS'])
    def inq_logout():
        if request.method == 'OPTIONS':
            return jsonify({'ok': True})
        _TOKENS.pop(_token_of_request(), None)
        return jsonify({'ok': True})

    @app.route('/api/inq/export', methods=['GET'])
    def inq_export():
        """관리자 백업용 — 비밀번호 해시는 제외"""
        if not _is_admin():
            return jsonify({'ok': False, 'msg': '권한이 없습니다'}), 403
        with _LOCK:
            d = _load()
        out = []
        for it in d['items']:
            c = dict(it)
            c.pop('pw', None)
            out.append(c)
        return jsonify({'ok': True, 'items': out})

    if _admin_pw():
        logger.info(f"[INQ] 문의 게시판 등록 완료 · 저장위치 {_FILE}")
    else:
        logger.warning("[INQ] 문의 게시판 등록 완료 (관리자 로그인 꺼짐) "
                       f"— 환경변수 GDSP_ADMIN_PW 또는 {_PW_FILE} 를 설정하세요")
    return app
