"""
G-DSP 로컬 프록시 서버
────────────────────────
브라우저(index.html)에서 직접 화성시 민원 사이트를 크롤링할 수 없는 이유:
  → CORS(Cross-Origin Resource Sharing) 보안 정책으로 차단됨

해결책: 이 Flask 서버를 로컬에서 실행하면
  index.html → localhost:5050/api/crawl → 화성시 서버
  의 경로로 크롤링 가능

실행 방법:
  pip install flask requests beautifulsoup4
  python server.py

그러면 http://localhost:5050 에서 G-DSP가 열립니다.
"""

from flask import Flask, request, jsonify, send_file
from flask import make_response
import os
import requests as req
from urllib.parse import urlparse

# hwaseong_crawl.py 와 같은 폴더에 있어야 함
try:
    from hwaseong_crawl import crawl_minwon
    HAS_CRAWL = True
except Exception:
    HAS_CRAWL = False
    def crawl_minwon(no):
        return {"ok": False, "error": "크롤러 모듈 없음"}

app = Flask(__name__)

# ── CORS 허용 (로컬 브라우저 → 이 서버) ──
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ── index.html 서빙 ──
@app.route("/")
def index():
    # server.py 와 같은 폴더의 index.html 을 서빙
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(html_path):
        return "index.html 파일이 없습니다. server.py 와 같은 폴더에 index.html 을 넣어주세요.", 404
    return send_file(html_path)


# ── 크롤링 API ──
@app.route("/api/crawl", methods=["POST", "OPTIONS"])
def api_crawl():
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(silent=True) or {}
    case_no = str(body.get("case_no", "")).strip()

    if not case_no:
        return jsonify({"ok": False, "error": "접수번호(case_no)가 필요합니다."}), 400

    if not case_no.isdigit():
        return jsonify({"ok": False, "error": "접수번호는 숫자만 입력해주세요."}), 400

    result = crawl_minwon(case_no)
    return jsonify(result)


# ── 여러 건 일괄 조회 ──
@app.route("/api/crawl/batch", methods=["POST", "OPTIONS"])
def api_crawl_batch():
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(silent=True) or {}
    case_nos = body.get("case_nos", [])  # ["번호1", "번호2", ...]

    if not case_nos or not isinstance(case_nos, list):
        return jsonify({"ok": False, "error": "case_nos 배열이 필요합니다."}), 400

    results = {}
    import time
    for no in case_nos[:20]:  # 한 번에 최대 20건
        no = str(no).strip()
        results[no] = crawl_minwon(no)
        time.sleep(0.8)  # 서버 부하 방지 (너무 빠르면 차단될 수 있음)

    return jsonify({"ok": True, "results": results})


# ── 서버 상태 확인 ──
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "message": "G-DSP 로컬 서버 실행 중"})


# ── 외부 API 프록시 (CORS 우회) ──
@app.route("/api/proxy", methods=["GET", "POST", "OPTIONS"])
def api_proxy():
    if request.method == "OPTIONS":
        return "", 204

    target_url = request.args.get("url") or (request.get_json(silent=True) or {}).get("url")
    if not target_url:
        return jsonify({"ok": False, "error": "url 파라미터가 필요합니다."}), 400

    # 허용 도메인만 통과 (보안)
    allowed = ["api.vworld.kr", "apis.data.go.kr", "api.data.go.kr"]
    host = urlparse(target_url).hostname or ""
    if not any(host == a or host.endswith("." + a) for a in allowed):
        return jsonify({"ok": False, "error": "허용되지 않은 도메인: " + host}), 403

    try:
        if request.method == "POST":
            resp = req.post(target_url, json=request.get_json(silent=True), timeout=10)
        else:
            resp = req.get(target_url, timeout=10)
        try:
            return jsonify(resp.json())
        except Exception:
            return resp.text, resp.status_code, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 55)
    print("  G-DSP 로컬 크롤링 서버")
    print("=" * 55)
    print("  브라우저에서 열기: http://localhost:5050")
    print("  종료: Ctrl+C")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5050, debug=False)
