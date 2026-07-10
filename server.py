"""
G-DSP Flask Server v3.0
- 레이어 분리: 지적경계/지번, 건물외곽/건물용도, 용도지역/명칭, 지구단위계획/명칭
- 6개 레이어: cadastra, building(NED WFS), road, road_center, zone_use, district_plan
- 도로경계: LT_C_UPISUQ151 (도시계획도로)
- 토지이용 + 등기부 + 통계 + 크롤링 등 기존 라우트 모두 유지
"""
from flask import Flask, request, jsonify, send_file, send_from_directory, make_response
from flask_cors import CORS
from flask_caching import Cache
import requests as req, ezdxf, io, logging, math, json, os, time, re
import xml.etree.ElementTree as ET
from ezdxf.enums import TextEntityAlignment
from pyproj import Transformer
from shapely.geometry import Point, box as Box, shape as shapely_shape, mapping as shapely_mapping
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
import base64
import numpy as np

app = Flask(__name__)
CORS(app, supports_credentials=True)
cache = Cache(app, config={'CACHE_TYPE': 'simple', 'CACHE_DEFAULT_TIMEOUT': 3600})
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VWORLD_KEY = "16B90D39-90BB-3197-987A-54983A46F250"
VWORLD_DOMAIN = "168-107-15-68.nip.io"
KAKAO_APP_KEY = "c670e0bc85874ef6267220f09882b379"  # REST API 키 (JS키 0f432d...와 다름!)

# ── KIGAM 수치지질도 ──────────────────────────────────────────────
KIGAM_KEY = "mzt5lyC51EuMLE1FlKz1Xvk7inmlKd"

# ── 흙토람(농진청) 토양특성 = 토심 ────────────────────────────────
# ★ http 전용(https는 Forbidden). PNU_CD 19자리로 조회. 유효토심 Vldsoildep_Cd(01~04)
SOIL_KEY = "09b819905e0a70316749fb91c03a216633ad3e75196a6aa25e6a9f273d9116f8"
# 유효토심코드 → 한글(흙토람 4등급과 동일)
SOIL_DEPTH_NAME = {'01':'매우얕음(0~25cm)','02':'얕음(25~50cm)','03':'보통(50~100cm)','04':'깊음(100cm이상)'}

@app.route('/api/kigam-wms')
@cache.cached(timeout=3600, query_string=True)
def kigam_wms_proxy():
    """KIGAM 수치지질도 WMS 프록시 (CORS 우회)
    URL: https://data.kigam.re.kr/openapi/wms
    파라미터: key=KEY + 표준WMS파라미터
    레이어: L_50K_Geology_Map / L_250K_Geology_Map
    """
    try:
        params = dict(request.args)
        params['key'] = KIGAM_KEY
        r = req.get(
            'https://data.kigam.re.kr/openapi/wms',
            params=params,
            headers={"Referer": "https://data.kigam.re.kr", "User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        resp = make_response(r.content)
        resp.headers['Content-Type'] = r.headers.get('Content-Type', 'image/png')
        resp.headers['Access-Control-Allow-Origin'] = '*'
        logger.info(f"[kigam-wms] {r.status_code} {len(r.content)}bytes")
        return resp
    except Exception as e:
        logger.error(f"[kigam-wms] {e}")
        return b'', 404

@app.route('/api/geo-map')
@cache.cached(timeout=3600, query_string=True)
def geo_map_proxy():
    """KIGAM 지질도 이미지 GetMap 프록시 (지형도 위에 겹치기용)
    엔드포인트: data.kigam.re.kr/mgeo/geoserver/wms (geoserver 본체, key 불필요)
    통과조건: User-Agent(Chrome) + Referer(data.kigam.re.kr/map/) + 레이어명 geoOpen: 네임스페이스
    params:
        bbox   — 'minLng,minLat,maxLng,maxLat' (EPSG:4326 WGS84). 지형도 5186은 프론트에서 변환해 전달
        w, h   — 이미지 픽셀 크기 (기본 800x600)
        scale  — '50k'(기본) | '250k'  → 레이어 선택
    returns: PNG (RGBA, 투명)
    """
    try:
        bbox = request.args.get('bbox', '')
        if not bbox or len(bbox.split(',')) != 4:
            return b'', 400
        w = request.args.get('w', '800')
        h = request.args.get('h', '600')
        layer = 'geoOpen:L_250K_Geology_Map' if request.args.get('scale') == '250k' else 'geoOpen:L_50K_Geology_Map'
        params = {
            'service': 'WMS', 'version': '1.1.1', 'request': 'GetMap',
            'layers': layer, 'srs': 'EPSG:4326', 'bbox': bbox,
            'width': w, 'height': h, 'format': 'image/png', 'transparent': 'true',
        }
        r = req.get(
            'https://data.kigam.re.kr/mgeo/geoserver/wms',
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": "https://data.kigam.re.kr/map/",
            },
            timeout=20
        )
        ct = r.headers.get('Content-Type', '')
        # 이미지가 아니면(에러 XML 등) 실패로 처리
        if 'image' not in ct or len(r.content) < 500:
            logger.warning(f"[geo-map] not image: {r.status_code} {ct} {len(r.content)}bytes")
            return b'', 502
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'image/png'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        logger.info(f"[geo-map] {r.status_code} {len(r.content)}bytes {layer}")
        return resp
    except Exception as e:
        logger.error(f"[geo-map] {e}")
        return b'', 404

@app.route('/api/geo-legend')
@cache.cached(timeout=86400, query_string=True)
def geo_legend_proxy():
    """KIGAM 지질도 범례 이미지 (GetLegendGraphic) — 암종별 색+이름
    scale: '50k'(기본) | '250k'
    returns: PNG (세로로 긴 범례)
    """
    try:
        layer = 'geoOpen:L_250K_Geology_Map' if request.args.get('scale') == '250k' else 'geoOpen:L_50K_Geology_Map'
        params = {
            'service': 'WMS', 'version': '1.1.1', 'request': 'GetLegendGraphic',
            'layer': layer, 'format': 'image/png',
        }
        r = req.get(
            'https://data.kigam.re.kr/mgeo/geoserver/wms',
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": "https://data.kigam.re.kr/map/",
            },
            timeout=20
        )
        ct = r.headers.get('Content-Type', '')
        if 'image' not in ct or len(r.content) < 500:
            logger.warning(f"[geo-legend] not image: {r.status_code} {ct} {len(r.content)}bytes")
            return b'', 502
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'image/png'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        logger.info(f"[geo-legend] {r.status_code} {len(r.content)}bytes {layer}")
        return resp
    except Exception as e:
        logger.error(f"[geo-legend] {e}")
        return b'', 404

@app.route('/api/soil-depth')
@cache.cached(timeout=86400, query_string=True)
def soil_depth_proxy():
    """흙토람 토양특성 → 유효토심 조회 (검토의견서 토심 항목)
    ★ http 전용 (https는 Forbidden). PNU_CD 19자리.
    params: pnu (19자리) — 우리 도구가 지적선택/검색에서 뽑은 PNU
    returns: JSON {ok, pnu, depth_code, depth_name, raw:{27종 코드}}
    """
    import xml.etree.ElementTree as ET
    try:
        pnu = (request.args.get('pnu') or '').strip()
        if len(pnu) != 19 or not pnu.isdigit():
            return jsonify({'ok': False, 'error': f'PNU 19자리 아님: {pnu}'}), 400
        url = 'http://apis.data.go.kr/1390802/SoilEnviron/SoilCharac/V3/getSoilCharacter'
        # 흙토람 API가 가끔 느림 → 타임아웃 30초 + 재시도 2회
        r = None; last_err = None
        for _try in range(3):
            try:
                r = req.get(url, params={'serviceKey': SOIL_KEY, 'PNU_CD': pnu}, timeout=30)
                break
            except Exception as te:
                last_err = te
                logger.warning(f"[soil-depth] try{_try+1} timeout/err pnu={pnu}: {te}")
                r = None
        if r is None:
            return jsonify({'ok': False, 'error': f'흙토람 응답 지연(재시도 실패): {last_err}', 'pnu': pnu})
        body = r.text or ''
        if r.status_code != 200 or '<item>' not in body:
            logger.warning(f"[soil-depth] {r.status_code} no item pnu={pnu} body={body[:120]}")
            return jsonify({'ok': False, 'error': '토양자료 없음(임야/특수지번일 수 있음)', 'pnu': pnu})
        root = ET.fromstring(body)
        item = root.find('.//item')
        if item is None:
            return jsonify({'ok': False, 'error': '항목 없음', 'pnu': pnu})
        raw = {ch.tag: (ch.text or '').strip() for ch in item}
        dc = raw.get('Vldsoildep_Cd', '')
        dn = SOIL_DEPTH_NAME.get(dc, '알수없음' if dc else '없음')
        logger.info(f"[soil-depth] pnu={pnu} depth={dc}({dn})")
        return jsonify({'ok': True, 'pnu': pnu, 'depth_code': dc, 'depth_name': dn, 'raw': raw})
    except Exception as e:
        logger.error(f"[soil-depth] {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/soil-map')
@cache.cached(timeout=3600, query_string=True)
def soil_map_proxy():
    """VWorld 유효토심 WMS 이미지 (지형 위에 겹치기용)
    레이어: lt_c_asitsoildep (유효토심) — 실데이터 확인됨
    params:
        bbox — 'minLat,minLng,maxLat,maxLng' (EPSG:4326, VWorld 1.3.0은 lat,lng 순). 프론트가 5186→WGS84 변환해 전달
        w, h — 픽셀 크기 (기본 800x600)
        layer — 'depth'(기본 유효토심) | 'drain'(배수) | 'deep'(심토토성) | 'stone'(자갈)
    returns: PNG (RGBA 투명)
    """
    try:
        bbox = request.args.get('bbox', '')
        if not bbox or len(bbox.split(',')) != 4:
            return b'', 400
        w = request.args.get('w', '800'); h = request.args.get('h', '600')
        lmap = {'depth':'lt_c_asitsoildep','drain':'lt_c_asitsoildra','deep':'lt_c_asitdeepsoil','stone':'lt_c_asitsurston'}
        layer = lmap.get(request.args.get('layer'), 'lt_c_asitsoildep')
        params = {
            'SERVICE':'WMS','REQUEST':'GetMap','VERSION':'1.3.0',
            'LAYERS':layer,'CRS':'EPSG:4326','BBOX':bbox,
            'WIDTH':w,'HEIGHT':h,'FORMAT':'image/png','TRANSPARENT':'true',
            'KEY':VWORLD_KEY,'DOMAIN':f'http://{VWORLD_DOMAIN}',
        }
        r = req.get('http://api.vworld.kr/req/wms', params=params,
                    headers={"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}, timeout=20)
        ct = r.headers.get('Content-Type', '')
        if 'image' not in ct or len(r.content) < 400:
            logger.warning(f"[soil-map] not image: {r.status_code} {ct} {len(r.content)}bytes body={r.text[:120]}")
            return b'', 502
        resp = make_response(r.content)
        resp.headers['Content-Type'] = 'image/png'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        logger.info(f"[soil-map] {r.status_code} {len(r.content)}bytes {layer}")
        return resp
    except Exception as e:
        logger.error(f"[soil-map] {e}")
        return b'', 404

@app.route('/api/kigam-info')
def kigam_featureinfo_proxy():
    """KIGAM GetFeatureInfo — 허가지선 centroid의 지질 정보 조회
    params:
        lng, lat  — WGS84 경위도 (직접 입력 시)
        x, y      — 국가TM 좌표 (EPSG:5186, DXF 세계좌표) → 서버에서 자동변환
    returns: JSON {ok, props, moam:{score, name}}

    여러 INFO_FORMAT(json/gml/html/plain)을 차례로 시도하고,
    JSON이 아니어도 정규식으로 지질기호를 추출한다.
    """
    import re as _re
    try:
        if request.args.get('lng'):
            lng = float(request.args['lng'])
            lat = float(request.args['lat'])
        else:
            tx = float(request.args.get('x', 0))
            ty = float(request.args.get('y', 0))
            lng, lat = tm5186_to_wgs84(tx, ty)

        d = 0.003
        base_params = {
            'key': KIGAM_KEY,
            'SERVICE': 'WMS', 'REQUEST': 'GetFeatureInfo', 'VERSION': '1.1.1',
            'LAYERS': 'L_50K_Geology_Map',
            'QUERY_LAYERS': 'L_50K_Geology_Map',
            'BBOX': f'{lng-d},{lat-d},{lng+d},{lat+d}',
            'WIDTH': '101', 'HEIGHT': '101', 'X': '50', 'Y': '50',
            'SRS': 'EPSG:4326',
        }
        # INFO_FORMAT 후보 순서대로 시도
        formats = ['application/json', 'application/vnd.ogc.gml', 'text/plain', 'text/html']
        last_body = ''
        last_ct = ''
        for fmt in formats:
            params = dict(base_params); params['INFO_FORMAT'] = fmt
            try:
                r = req.get('https://data.kigam.re.kr/openapi/wms', params=params,
                            headers={"Referer": "https://data.kigam.re.kr",
                                     "User-Agent": "Mozilla/5.0"}, timeout=12)
            except Exception as fe:
                logger.warning(f"[kigam-info] {fmt} request failed: {fe}")
                continue
            body = r.text or ''
            ct = r.headers.get('Content-Type', '')
            last_body, last_ct = body, ct
            if not body.strip():
                continue  # 빈 응답 → 다음 포맷

            props = {}
            # 1) JSON 파싱 시도
            if 'json' in ct.lower() or body.lstrip().startswith('{'):
                try:
                    data = r.json()
                    feats = data.get('features', [])
                    if feats:
                        props = feats[0].get('properties', {}) or {}
                except Exception:
                    pass
            # 2) GML/XML 파싱 (태그값 추출)
            if not props and ('<' in body):
                # <기호>Kgr</기호> 또는 <기호 ...>Kgr</...> 형태
                for key in ['기호', '지층', '대표암성', 'SYMBOL', 'CODE', 'SYM', 'RNAME', 'LEGEND']:
                    m = _re.search(r'<[^>]*' + key + r'[^>]*>([^<]+)<', body, _re.IGNORECASE)
                    if m:
                        props[key] = m.group(1).strip()
            # 3) text/plain 파싱 (key=value 또는 key: value)
            if not props and '=' in body:
                for line in body.splitlines():
                    mm = _re.match(r'\s*([\w가-힣]+)\s*[=:]\s*(.+)', line)
                    if mm:
                        props[mm.group(1).strip()] = mm.group(2).strip()

            if props:
                code = str(props.get('기호', props.get('CODE', props.get('SYM',
                          props.get('SYMBOL', props.get('LEGEND', '')))))).strip()
                rock = str(props.get('대표암성', props.get('RNAME',
                          props.get('지층', '')))).strip()
                moam = classify_moam(code, rock)
                return jsonify({'ok': True, 'props': props, 'moam': moam,
                                'wgs84': [lng, lat], 'fmt': fmt})

        # 모든 포맷 실패 → 디버그 정보 포함
        snippet = (last_body or '').strip()[:160]
        logger.warning(f"[kigam-info] no props. ct={last_ct} body={snippet!r}")
        return jsonify({'ok': False,
                        'msg': '지질 정보를 읽지 못했습니다 (응답 형식 불명)',
                        'debug': {'ct': last_ct, 'snippet': snippet}})
    except Exception as e:
        logger.error(f"[kigam-info] {e}")
        return jsonify({'ok': False, 'msg': str(e)}), 500


def tm5186_to_wgs84(x, y):
    """EPSG:5186 (Korean Central Belt 2010) → WGS84 경위도
    pyproj Transformer 사용 (정밀). 모듈 하단 _TM5186_TO_WGS84 정의됨.
    """
    lng, lat = _TM5186_TO_WGS84.transform(x, y)
    return lng, lat


def classify_moam(code, rock=''):
    """지질 기호/암석명 → 재해위험성 모암 채점표 분류
    채점: 퇴적암=0, 화성암기타=5, 변성암천매암=12, 변성암편마암=19, 화성암반암=56
    """
    c = (code or '').lower().strip()
    r = (rock or '').lower()
    full = c + ' ' + r
    # 1) 안산암류/화산암/반암 (56점) — 가장 높은 위험
    if any(k in full for k in ['andesite','basalt','rhyolite','porphyr','trachyte','dacite',
                                'volcanic','tuff','안산암','현무암','유문암','반암','조면암','응회암','화산']):
        return {'score':56,'name':'화성암(반암류·안산암류)'}
    if 'gr' not in c and any(k in c for k in ['an','ba','bs','vo','rh','da','tr']):
        return {'score':56,'name':'화성암(반암류·안산암류)'}
    # 2) 편마암/편암 (19점)
    if any(k in full for k in ['gneiss','schist','편마암','편암']) or 'gn' in c or 'sch' in c:
        return {'score':19,'name':'변성암(편마암류·편암류)'}
    # 3) 천매암/점판암 (12점)
    if any(k in full for k in ['phyllite','slate','천매암','점판암','슬레이트']) or c.endswith('sl') or 'ph' in c:
        return {'score':12,'name':'변성암(천매암·점판암 기타)'}
    # 4) 화강암류 (5점)
    if any(k in full for k in ['granite','diorite','gabbro','tonalite','monzonite','syenite',
                                '화강암','섬록암','반려암','화강']) or 'gr' in c or (c[:1]=='g'):
        return {'score':5,'name':'화성암(화강암류 기타)'}
    # 5) 퇴적암 (0점) — 기본값
    return {'score':0,'name':'퇴적암(이암·석회암·사암 등)'}


BUILDING_KEY = "09b819905e0a70316749fb91c03a216633ad3e75196a6aa25e6a9f273d9116f8"  # 국토부 건축물대장
_WGS84_TO_TM5186 = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True)
_TM5186_TO_WGS84 = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True)

# ==================== 레이어 정의 ====================
# 각 레이어: line_layer(폴리곤/선) + text_layer(명칭) 분리
VWORLD_LAYERS = {
    'cadastra': {
        'endpoint': 'data', 'data_name': 'LP_PA_CBND_BUBUN',
        'line_layer': '지적경계', 'line_color': 1,
        'text_layer': '지번', 'text_color': 7,
        'text_fields': ['jibun'],
        'cad_name': '지적선', 'color': 1,  # 호환성
    },
    'building': {
        'endpoint': 'ned_wfs', 'data_name': 'getBuildingUseWFS',
        'line_layer': '건물외곽', 'line_color': 3,
        'text_layer': '건물용도', 'text_color': 6,
        'text_fields': ['main_prpos_code_nm', 'ground_floor_co'],
        'cad_name': '건물', 'color': 3,
    },
    'road': {
        'endpoint': 'data', 'data_name': 'LT_C_UPISUQ151',  # 도시계획도로
        'line_layer': '도로경계', 'line_color': 5,
        'text_layer': None, 'text_color': None,
        'text_fields': [],
        'cad_name': '도로경계', 'color': 5,
    },
    'road_center': {
        'endpoint': 'data', 'data_name': 'LT_L_SPRD',
        'line_layer': '도로중심선', 'line_color': 8,
        'text_layer': None, 'text_color': None,
        'text_fields': [],
        'cad_name': '도로중심선', 'color': 8,
    },
    'zone_use': {
        'endpoint': 'data', 'data_name': 'LT_C_UQ111',
        'line_layer': '용도지역', 'line_color': 4,
        'text_layer': '용도지역명', 'text_color': 4,
        'text_fields': ['uname'],
        'cad_name': '용도지역', 'color': 4,
    },
    'district_plan': {
        'endpoint': 'data', 'data_name': 'LT_C_UQ121',
        'line_layer': '지구단위계획', 'line_color': 30,
        'text_layer': '지구단위명', 'text_color': 30,
        'text_fields': ['uname'],
        'cad_name': '지구단위계획', 'color': 30,
    },
    # 옛날 호환
    'zoning':    {'endpoint': 'data', 'data_name': 'LT_C_UQ111', 'line_layer': '용도지역', 'line_color': 4, 'text_layer': '용도지역명', 'text_color': 4, 'text_fields': ['uname'], 'cad_name': '용도지역', 'color': 4},
    'greenbelt': {'endpoint': 'data', 'data_name': 'LT_C_UO0110000', 'line_layer': '개발제한구역', 'line_color': 2, 'text_layer': None, 'text_color': None, 'text_fields': [], 'cad_name': '개발제한구역', 'color': 2},
    'farmland':  {'endpoint': 'data', 'data_name': 'LT_C_UQ121', 'line_layer': '농업진흥지역', 'line_color': 6, 'text_layer': None, 'text_color': None, 'text_fields': [], 'cad_name': '농업진흥지역', 'color': 6},
    'hazard':    {'endpoint': 'data', 'data_name': 'LT_C_UO0117000', 'line_layer': '재해위험지구', 'line_color': 4, 'text_layer': None, 'text_color': None, 'text_fields': [], 'cad_name': '재해위험지구', 'color': 4},
}


# ==================== VWorld 데이터 가져오기 ====================
def _fetch_vworld_box(vworld_data, bbox, size=1000):
    """일반 VWorld data API 호출 (⭐페이징: 모든 피처 다 받을 때까지 → 구멍 방지)"""
    url = "https://api.vworld.kr/req/data"
    headers = {"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}
    all_features = []
    page = 1
    max_pages = 10  # 안전장치 (1000×10=최대 1만개)
    while page <= max_pages:
        params = {
            "service": "data", "request": "GetFeature", "data": vworld_data,
            "key": VWORLD_KEY, "format": "json", "size": str(size), "page": str(page),
            "domain": VWORLD_DOMAIN,
            "geomFilter": f"BOX({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]})",
        }
        try:
            r = req.get(url, params=params, headers=headers, timeout=25)
            data = r.json()
            fc = data.get("response", {}).get("result", {}).get("featureCollection", {})
            feats = (fc.get("features", []) or []) if fc else []
            all_features.extend(feats)
            # 받은 개수가 size 미만이면 마지막 페이지
            if len(feats) < size:
                break
            page += 1
        except Exception as e:
            logger.error(f"[WFS {vworld_data}] p{page} {e}")
            break
    return all_features


def _fetch_ned_wfs_box(endpoint_name, bbox, size=500):
    """NED WFS GML 응답 파싱 (건물용)"""
    url = f"https://api.vworld.kr/ned/wfs/{endpoint_name}"
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    params = {
        "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
        "typename": "dt_d198",
        "srsname": "EPSG:4326",
        "output": "GML2",
        "maxFeatures": str(size),
        "bbox": bbox_str
    }
    headers = {"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}
    try:
        r = req.get(url, params=params, headers=headers, timeout=30)
        r.encoding = 'utf-8'
        root = ET.fromstring(r.text)
        ns = {'gml': 'http://www.opengis.net/gml', 'sop': 'https://www.vworld.kr', 'wfs': 'http://www.opengis.net/wfs'}
        features = []
        for member in root.findall('.//gml:featureMember', ns):
            ag_geom = member.find('.//sop:ag_geom', ns)
            if ag_geom is None:
                continue
            multi_poly = ag_geom.find('.//gml:MultiPolygon', ns)
            polygons_coords = []
            if multi_poly is not None:
                for poly_member in multi_poly.findall('.//gml:polygonMember', ns):
                    ring = poly_member.find('.//gml:LinearRing', ns)
                    if ring is not None:
                        coords_elem = ring.find('gml:coordinates', ns)
                        if coords_elem is not None and coords_elem.text:
                            try:
                                points = [[float(x) for x in pair.split(',')] for pair in coords_elem.text.strip().split()]
                                if len(points) >= 3:
                                    polygons_coords.append(points)
                            except:
                                pass
            else:
                ring = ag_geom.find('.//gml:LinearRing', ns)
                if ring is not None:
                    coords_elem = ring.find('gml:coordinates', ns)
                    if coords_elem is not None and coords_elem.text:
                        try:
                            points = [[float(x) for x in pair.split(',')] for pair in coords_elem.text.strip().split()]
                            if len(points) >= 3:
                                polygons_coords.append(points)
                        except:
                            pass
            if not polygons_coords:
                continue
            props = {}
            for child in member.iter():
                if child.tag.startswith('{https://www.vworld.kr}') and child.text:
                    key = child.tag.split('}')[1]
                    if key not in ('ag_geom',):
                        props[key] = child.text.strip()
            if len(polygons_coords) > 1:
                features.append({'geometry': {'type': 'MultiPolygon', 'coordinates': [[ring] for ring in polygons_coords]}, 'properties': props})
            else:
                features.append({'geometry': {'type': 'Polygon', 'coordinates': [polygons_coords[0]]}, 'properties': props})
        return features
    except Exception as e:
        logger.error(f"NED WFS {endpoint_name} 에러: {e}")
        return []


def _fetch_layer_all(layer_key, bbox):
    """레이어별 데이터 가져오기 (4분할 병렬)"""
    info = VWORLD_LAYERS[layer_key]
    minx, miny, maxx, maxy = bbox
    midx, midy = (minx+maxx)/2, (miny+maxy)/2
    sub_bboxes = [
        [minx, miny, midx, midy], [midx, miny, maxx, midy],
        [minx, midy, midx, maxy], [midx, midy, maxx, maxy]
    ]
    fetch_fn = _fetch_ned_wfs_box if info['endpoint'] == 'ned_wfs' else _fetch_vworld_box
    
    combined = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(fetch_fn, info['data_name'], sb, 500) for sb in sub_bboxes]
        for fut in concurrent.futures.as_completed(futures):
            try:
                items = fut.result() or []
                for f in items:
                    fid = f.get("id") or f.get("properties", {}).get("pnu") or f.get("properties", {}).get("gis_idntfc_no") or str(f.get("geometry"))[:80]
                    combined[fid] = f
            except Exception as e:
                logger.error(f"[{layer_key}] 분할 실패: {e}")
    return list(combined.values())


# ==================== DXF 그리기 ====================
def _wgs84_to_cad(lng, lat):
    return _WGS84_TO_TM5186.transform(lng, lat)


def _convert_ring(coords):
    out = []
    for p in coords:
        try:
            x, y = _wgs84_to_cad(float(p[0]), float(p[1]))
            out.append((x, y))
        except:
            pass
    return out


def _add_geom_to_msp(msp, geom, layer_name):
    """geometry → DXF 폴리라인 (Polygon/MultiPolygon/LineString/MultiLineString 지원)"""
    if not geom:
        return 0
    gtype = geom.get('type')
    coords = geom.get('coordinates', [])
    cnt = 0
    try:
        if gtype == 'Polygon':
            for ring in coords:
                pts = _convert_ring(ring)
                if len(pts) >= 3:
                    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer_name})
                    cnt += 1
        elif gtype == 'MultiPolygon':
            for poly in coords:
                for ring in poly:
                    pts = _convert_ring(ring)
                    if len(pts) >= 3:
                        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer_name})
                        cnt += 1
        elif gtype == 'LineString':
            pts = _convert_ring(coords)
            if len(pts) >= 2:
                msp.add_lwpolyline(pts, close=False, dxfattribs={"layer": layer_name})
                cnt += 1
        elif gtype == 'MultiLineString':
            for line in coords:
                pts = _convert_ring(line)
                if len(pts) >= 2:
                    msp.add_lwpolyline(pts, close=False, dxfattribs={"layer": layer_name})
                    cnt += 1
    except Exception as e:
        logger.error(f"[GEOM] {layer_name} {gtype} 실패: {e}")
    return cnt


def _geom_centroid(geom):
    try:
        g = shapely_shape(geom)
        c = g.centroid
        return c.x, c.y
    except:
        return None, None


# ==================== 메인 DXF 엔드포인트 ====================
@app.route("/api/wfs-dxf-circle", methods=["POST", "OPTIONS"])
def wfs_dxf_circle():
    if request.method == "OPTIONS":
        return "", 200
    t0 = time.time()
    try:
        body = request.get_json(force=True) or {}
        # lng/lat 또는 center_lng/center_lat 둘 다 받기
        center_lng = float(body.get("center_lng") or body.get("lng"))
        center_lat = float(body.get("center_lat") or body.get("lat"))
    except:
        return jsonify({"error": "lng/lat 또는 center_lng/center_lat 필요"}), 400

    radius_m = float(body.get("radius", 500))
    radius_m = max(0, min(2000, radius_m))  # 0 = 선택필지만
    requested = body.get("layers") or ["cadastra"]
    layer_ids = [l for l in requested if l in VWORLD_LAYERS]
    if not layer_ids:
        return jsonify({"error": "유효한 레이어 없음"}), 400
    jibun = (body.get("jibun") or "").strip()
    # ⭐ 복수 필지 PNU 목록 (선택필지만 모드용)
    pnus = body.get("pnus") or []
    if isinstance(pnus, str): pnus = [pnus]
    # ⭐ 직접선택 영역 폴리곤 (GeoJSON, 경위도)
    draw_polygon = None
    try:
        pg = body.get("polygon")
        if pg and pg.get("coordinates"):
            draw_polygon = shapely_shape(pg)
    except:
        draw_polygon = None

    # ⭐ 선택 필지 geometry들 (외곽 반경 기준용)
    parcel_union = None
    try:
        pgs = body.get("parcel_geoms") or []
        geoms = []
        for g in pgs:
            try: geoms.append(shapely_shape(g))
            except: pass
        if geoms:
            from shapely.ops import unary_union
            parcel_union = unary_union(geoms)
    except:
        parcel_union = None

    # bbox 계산 (도(deg) 단위)
    if draw_polygon is not None:
        # 직접선택: 그린 영역 bbox + 여유
        pminx, pminy, pmaxx, pmaxy = draw_polygon.bounds
        pad = 0.003
        bbox = [pminx - pad, pminy - pad, pmaxx + pad, pmaxy + pad]
        circle_filter = draw_polygon  # ⭐그린 영역에 걸치면 포함
    elif radius_m <= 0:
        # 선택필지만: 필지 bbox + 여유
        if parcel_union is not None:
            pminx, pminy, pmaxx, pmaxy = parcel_union.bounds
            pad = 0.003
            bbox = [pminx - pad, pminy - pad, pmaxx + pad, pmaxy + pad]
        else:
            eff_r = 150.0
            lat_deg = (eff_r + 300) / 111000.0
            lng_deg = (eff_r + 300) / (111000.0 * max(0.1, math.cos(math.radians(center_lat))))
            bbox = [center_lng - lng_deg, center_lat - lat_deg, center_lng + lng_deg, center_lat + lat_deg]
        circle_filter = None  # pnus 모드에서 사용 안함
    else:
        # ⭐ 반경 모드: 필지 외곽에서 radius_m 버퍼 (중심 아님!)
        radius_deg = radius_m / 111000.0
        if parcel_union is not None:
            circle_filter = parcel_union.buffer(radius_deg)
        else:
            circle_filter = Point(center_lng, center_lat).buffer(radius_deg * 1.15)
        fminx, fminy, fmaxx, fmaxy = circle_filter.bounds
        pad = 0.002
        bbox = [fminx - pad, fminy - pad, fmaxx + pad, fmaxy + pad]

    # 레이어별 병렬 가져오기
    t_fetch_start = time.time()
    features_by_layer = {}
    with ThreadPoolExecutor(max_workers=max(1, len(layer_ids))) as ex:
        futures = {ex.submit(_fetch_layer_all, lid, bbox): lid for lid in layer_ids}
        for fut in concurrent.futures.as_completed(futures):
            lid = futures[fut]
            try:
                features_by_layer[lid] = fut.result() or []
            except Exception as e:
                logger.error(f"[FETCH] {lid} 실패: {e}")
                features_by_layer[lid] = []
    fetch_time = time.time() - t_fetch_start

    # 영역 필터링
    # - 직접선택(draw_polygon): ⭐그린 영역에 걸치면 전부 포함
    # - 선택필지만(radius=0): PNU 목록과 일치하는 필지만
    # - 반경 모드: ⭐원에 조금이라도 걸치면 포함 (intersects - 구멍 방지)
    sel_union = None
    use_pnus_mode = (draw_polygon is None) and (radius_m <= 0) and bool(pnus)
    if use_pnus_mode:
        # 선택 필지 geometry 먼저 수집 (cadastra에서)
        sel_geoms = []
        for f in features_by_layer.get('cadastra', []):
            p = (f.get('properties') or {})
            if str(p.get('pnu') or '') in [str(x) for x in pnus]:
                try: sel_geoms.append(shapely_shape(f.get('geometry')))
                except: pass
        if sel_geoms:
            from shapely.ops import unary_union
            sel_union = unary_union(sel_geoms).buffer(0.00001)  # 약간 버퍼(경계 맞닿음 포함)

    for lid in layer_ids:
        filtered = []
        for f in features_by_layer.get(lid, []):
            try:
                g = shapely_shape(f.get('geometry'))
                if use_pnus_mode:
                    if lid == 'cadastra':
                        p = (f.get('properties') or {})
                        if str(p.get('pnu') or '') in [str(x) for x in pnus]:
                            filtered.append(f)
                    elif sel_union is not None and g.intersects(sel_union):
                        filtered.append(f)
                else:
                    if circle_filter is not None and g.intersects(circle_filter):
                        filtered.append(f)
                    elif circle_filter is None:
                        filtered.append(f)
            except:
                filtered.append(f)
        features_by_layer[lid] = filtered

    # DXF 생성
    doc = ezdxf.new('R2010', setup=True)
    msp = doc.modelspace()
    if 'HANGUL' not in doc.styles:
        try:
            doc.styles.new('HANGUL', dxfattribs={'font': 'malgun.ttf', 'width': 0.8})
        except:
            pass

    total = 0
    layer_stats = {}
    for lid in layer_ids:
        info = VWORLD_LAYERS[lid]
        line_layer = info['line_layer']
        text_layer = info.get('text_layer')
        text_fields = info.get('text_fields', [])
        
        if line_layer not in doc.layers:
            doc.layers.new(name=line_layer, dxfattribs={"color": info['line_color']})
        if text_layer and text_layer not in doc.layers:
            doc.layers.new(name=text_layer, dxfattribs={"color": info.get('text_color', 7)})

        cnt = 0
        text_count = 0
        for f in features_by_layer.get(lid, []):
            cnt += _add_geom_to_msp(msp, f.get("geometry"), line_layer)
            
            # 텍스트 라벨
            if text_layer and text_fields:
                props = f.get("properties") or {}
                label_parts = []
                for fld in text_fields:
                    v = props.get(fld)
                    if v and str(v).strip():
                        label_parts.append(str(v).strip())
                label = ' '.join(label_parts)
                if label:
                    cx, cy = _geom_centroid(f.get("geometry"))
                    if cx is not None:
                        try:
                            nx, ny = _wgs84_to_cad(cx, cy)
                            text_height = 2.0 if lid == 'cadastra' else 3.0
                            t = msp.add_text(label, dxfattribs={
                                "layer": text_layer,
                                "height": text_height,
                                "style": "HANGUL",
                                "color": info.get('text_color', 7)
                            })
                            t.set_placement((nx, ny), align=TextEntityAlignment.MIDDLE_CENTER)
                            text_count += 1
                        except Exception as e:
                            pass

        layer_stats[line_layer] = cnt
        if text_layer:
            layer_stats[text_layer] = text_count
        total += cnt

    # 기준점/노트 제거 (영대님 요청: 선택한 데이터 외 아무것도 안 넣음)

    # DXF 직렬화
    stream = io.StringIO()
    doc.write(stream)
    stream.seek(0)
    dxf_bytes = io.BytesIO(stream.read().encode('utf-8'))

    total_time = time.time() - t0
    logger.info(f"[DXF] {jibun or 'parcel'} r={int(radius_m)} feats={total} fetch={fetch_time:.1f}s total={total_time:.1f}s stats={layer_stats}")

    safe_name = jibun.replace(' ', '_') if jibun else 'parcel'
    fname = f"gdsp_{safe_name}_r{int(radius_m)}m.dxf"
    resp = make_response(send_file(dxf_bytes, mimetype='application/dxf', as_attachment=True, download_name=fname))
    resp.headers['X-Feature-Count'] = str(total)
    resp.headers['Access-Control-Expose-Headers'] = 'X-Feature-Count'
    return resp


# ==================== 기타 라우트 ====================
@app.route("/api/layers", methods=["GET"])
def list_layers():
    return jsonify({
        key: {
            'line_layer': v.get('line_layer'),
            'text_layer': v.get('text_layer'),
            'data': v.get('data_name')
        }
        for key, v in VWORLD_LAYERS.items()
    })


@app.route('/api/vw-tile/<map_type>/<int:z>/<int:x>/<int:y>.jpeg')
@cache.cached(timeout=86400, query_string=True)
def vw_tile_proxy(map_type, z, x, y):
    """OpenLayers는 z/x/y로 호출, VWorld는 z/y/x로 받음"""
    try:
        url = f"https://api.vworld.kr/req/wmts/1.0.0/{VWORLD_KEY}/{map_type}/{z}/{y}/{x}.jpeg"
        r = req.get(url, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=10)
        return r.content, 200, {'Content-Type': 'image/jpeg'}
    except:
        return b'', 404


@app.route('/api/vw-wms')
@cache.cached(timeout=3600, query_string=True)
def vw_wms_proxy():
    try:
        params = dict(request.args)
        params['key'] = VWORLD_KEY
        params['domain'] = VWORLD_DOMAIN
        url = "https://api.vworld.kr/req/wms"
        r = req.get(url, params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=10)
        return r.content, 200, {'Content-Type': r.headers.get('Content-Type', 'image/png')}
    except:
        return b'', 404


@app.route('/api/wfs')
def wfs_parcel():
    try:
        pnu = request.args.get('pnu')
        lng = request.args.get('lng')
        lat = request.args.get('lat')
        params = {
            "service": "data", "request": "GetFeature",
            "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
            "format": "json", "size": "1"
        }
        if pnu:
            params["attrFilter"] = f"pnu:=:{pnu}"
        elif lng and lat:
            f = 0.0001
            params["geomFilter"] = f"BOX({float(lng)-f},{float(lat)-f},{float(lng)+f},{float(lat)+f})"
        else:
            return jsonify({"error": "pnu 또는 lng/lat 필요"}), 400
        r = req.get("https://api.vworld.kr/req/data", params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        data = r.json()
        result = data.get("response", {}).get("result", {})
        fc = result.get("featureCollection", {})
        features = fc.get("features", [])
        return jsonify({"features": features, "totalFeatures": len(features)})
    except Exception as e:
        return jsonify({"error": str(e), "features": []}), 500


@app.route('/api/landinfo')
def landinfo():
    try:
        pnu = request.args.get('pnu', '')
        if not pnu or len(pnu) < 19:
            return jsonify({"error": "PNU 필요"}), 400
        url = "https://api.vworld.kr/ned/data/getLandCharacteristics"
        params = {
            "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
            "format": "json", "numOfRows": "30", "pageNo": "1", "pnu": pnu
        }
        r = req.get(url, params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        data = r.json()
        # 토지특성은 연도별 이력이 누적됨. numOfRows=1이면 옛 연도가 잡혀 작년 공시지가가 떴음.
        # 최신 기준연도(stdrYear)가 맨 앞에 오도록 정렬 → 프런트가 field[0]에서 올해 값을 읽음.
        try:
            lc = data.get("landCharacteristicss") if isinstance(data, dict) else None
            fld = lc.get("field") if isinstance(lc, dict) else None
            if isinstance(fld, list) and len(fld) > 1:
                fld.sort(key=lambda x: str(x.get("stdrYear", "")), reverse=True)
                data["landCharacteristicss"]["field"] = fld
        except Exception:
            pass
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/vworld')
def vworld_proxy():
    try:
        path = request.args.get('path', 'req/data')
        params = dict(request.args)
        params.pop('path', None)
        params['key'] = VWORLD_KEY
        params['domain'] = VWORLD_DOMAIN
        url = f"https://api.vworld.kr/{path}"
        r = req.get(url, params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        try:
            return jsonify(r.json())
        except:
            return r.content, r.status_code, {'Content-Type': r.headers.get('Content-Type', 'application/json')}
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/vw-diag')
def vw_diag():
    """VWorld 응답 진단 — 토양 등 WMS가 실제로 뭘 주는지 흑백 판정.
    path(기본 req/wms) + 나머지 WMS 파라미터 그대로 전달, 응답 분석해서 리턴."""
    import io
    try:
        path = request.args.get('path', 'req/wms')
        params = dict(request.args)
        params.pop('path', None)
        params['key'] = VWORLD_KEY
        params['domain'] = VWORLD_DOMAIN
        url = f"https://api.vworld.kr/{path}"
        r = req.get(url, params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        ct = r.headers.get('Content-Type', '')
        out = {
            "request_url": r.url,
            "status_code": r.status_code,
            "content_type": ct,
            "bytes": len(r.content),
        }
        if 'image' in ct:
            out["is_image"] = True
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(r.content)).convert("RGBA")
                data = list(im.getdata())
                non_blank = 0
                sample = {}
                for (rr, gg, bb, aa) in data:
                    if aa > 10 and not (rr > 245 and gg > 245 and bb > 245):
                        non_blank += 1
                        k = f"{rr},{gg},{bb}"
                        sample[k] = sample.get(k, 0) + 1
                out["total_px"] = len(data)
                out["colored_px"] = non_blank
                out["top_colors"] = sorted(sample.items(), key=lambda x: -x[1])[:8]
                out["verdict"] = ("데이터 있음 (색칠된 픽셀 존재)"
                                  if non_blank > 50 else "빈 이미지 (투명/흰색만)")
            except Exception as pe:
                out["pixel_check"] = (f"PIL 실패: {pe} → bytes로 판단 "
                                      f"({'데이터 가능성' if len(r.content) > 3000 else '빈 이미지 가능성'})")
        else:
            out["is_image"] = False
            out["text_preview"] = r.text[:800]
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _analyze_resp(r):
    """HTTP 응답을 분석 — 이미지면 색칠 픽셀 검사, 아니면 텍스트 미리보기."""
    import io
    ct = r.headers.get('Content-Type', '')
    out = {"status_code": r.status_code, "content_type": ct, "bytes": len(r.content)}
    if 'image' in ct:
        out["is_image"] = True
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            data = list(im.getdata())
            non_blank = 0
            sample = {}
            for (rr, gg, bb, aa) in data:
                if aa > 10 and not (rr > 245 and gg > 245 and bb > 245):
                    non_blank += 1
                    k = f"{rr},{gg},{bb}"
                    sample[k] = sample.get(k, 0) + 1
            out["total_px"] = len(data)
            out["colored_px"] = non_blank
            out["top_colors"] = sorted(sample.items(), key=lambda x: -x[1])[:8]
            out["verdict"] = ("데이터 있음 (색칠된 픽셀 존재)"
                              if non_blank > 50 else "빈 이미지 (투명/흰색만)")
        except Exception as pe:
            out["pixel_check"] = (f"PIL 실패: {pe} → bytes로 판단 "
                                  f"({'데이터 가능성' if len(r.content) > 3000 else '빈 이미지 가능성'})")
    else:
        out["is_image"] = False
        out["text_preview"] = r.text[:800]
    return out


@app.route('/api/kigam-diag')
def kigam_diag():
    """KIGAM 지질도 WMS 진단 — Referer 2종(data.kigam / nip.io) 둘 다 시도해
    어느 게 진짜 이미지를 주는지 한 방에 판정. (표시 미확인 원인 규명용)"""
    try:
        params = dict(request.args)
        params['key'] = KIGAM_KEY
        results = {"_request_url_sample": None}
        for label, ref in [("A_referer_kigam", "https://data.kigam.re.kr"),
                           ("B_referer_nipio", f"https://{VWORLD_DOMAIN}")]:
            try:
                r = req.get('https://data.kigam.re.kr/openapi/wms', params=params,
                            headers={"Referer": ref, "User-Agent": "Mozilla/5.0"}, timeout=20)
                info = _analyze_resp(r)
                info["referer_used"] = ref
                if results["_request_url_sample"] is None:
                    results["_request_url_sample"] = r.url
                results[label] = info
            except Exception as fe:
                results[label] = {"error": str(fe), "referer_used": ref}
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# iros.go.kr 프록시 (인터넷등기소 소유자 조회)
@app.route('/api/iros/list', methods=['POST', 'OPTIONS'])
def iros_proxy_list():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(force=True) or {}
        url = 'https://www.iros.go.kr/biz/Pr20ViaRlrgSrchCtrl/retrieveSmplSrchList.do?IS_NMBR_LOGIN__=null'
        r = req.post(url, json=body, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}, timeout=60)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/iros/cont', methods=['POST', 'OPTIONS'])
def iros_proxy_cont():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(force=True) or {}
        url = 'https://www.iros.go.kr/biz/Pr20ViaRlrgSrchCtrl/retrievePinSrchCont.do?IS_NMBR_LOGIN__=null'
        r = req.post(url, json=body, headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}, timeout=60)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== 토지이음 GIS 프록시 (CORS 우회) ====================
# UOC100=문화재보호구역, UOC200=문화재, UOC800=역사문화환경보존지역
# 좌표계: EPSG:5179 (UTM-K)
@app.route('/api/eum-proxy')
def eum_proxy():
    """토지이음 GIS 시스템 프록시 — 문화재 등 토지이용 규제 레이어 GeoJSON 반환"""
    layer = request.args.get('layer', 'BA')
    mbr = request.args.get('mbr', '')
    code = request.args.get('code', '')
    version = request.args.get('version', '20260414')

    if not mbr or not code:
        return jsonify({"error": "mbr and code parameters required"}), 400

    eum_url = (
        'https://www.eum.ne.kr:9001/MapPlan/MapPlan'
        f'?req=search&version={version}&layer={layer}'
        f'&mbr={mbr}&code={code}'
    )

    try:
        # 토지이음 SSL 인증서 검증 비활성화
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        r = req.get(eum_url, timeout=15, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            response = make_response(r.text)
            response.headers['Content-Type'] = 'application/json; charset=utf-8'
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
        return jsonify({"error": f"eum returned {r.status_code}"}), 502
    except Exception as e:
        logger.exception("eum_proxy 오류")
        return jsonify({"error": str(e)}), 500


# ============================================================
# 수치지형도 R12 변환 엔드포인트 (v2 — 빈화면/속도 개선)
#  - popup_dxf.html(도면·변환 탭)의 /api/convert-dxf 호출 처리
#  - mode='analysis': E/F/G 계열만 (경사도+수계 공용) / 'original': 전체
#  - LWPOLYLINE -> R12 POLYLINE, 등고선 elevation은 3D 폴리라인 z로 보존
#  - 엔티티가 실제 들어간 레이어만 테이블 생성 (유령 레이어 잔재 제거)
#  - INSERT z(표고점 실제표고) 보존
#  - ★ VPORT + EXTMIN/MAX 설정: 캐드에서 열자마자 도면이 보이게 (빈 화면 방지)
#
#  > server.py 의 @app.route("/api/health") 바로 위에 붙여넣으세요.
#    (Flask, request, make_response, send_file, jsonify, ezdxf, io 이미 import됨)
#    추가로 맨 위 import에 `import re` 가 필요합니다 (없으면 한 줄 추가).
# ============================================================
import re as _re_ngii

_NGII_ANALYSIS_FIRST = {'E', 'F', 'G'}
_NGII_EXCLUDE_PREFIX = ('F003', 'F004')


def _ngii_is_analysis_layer(name):
    if not name or name in ('0', 'Defpoints'):
        return False
    if any(name.startswith(p) for p in _NGII_EXCLUDE_PREFIX):
        return False
    return name[0].upper() in _NGII_ANALYSIS_FIRST


def _ngii_fix_extents(txt, varname, x, y):
    """저장된 R12 텍스트에서 $EXTMIN/$EXTMAX의 1e+20 더미값을 실제값으로 치환."""
    pat = _re_ngii.compile(
        r'(\$' + varname + r'\s*\n\s*10\s*\n)[^\n]*(\n\s*20\s*\n)[^\n]*(\n\s*30\s*\n)[^\n]*')
    return pat.sub(lambda m: f"{m.group(1)}{x}{m.group(2)}{y}{m.group(3)}0.0", txt)


@app.route("/api/convert-dxf", methods=["POST", "OPTIONS"])
def convert_dxf():
    if request.method == "OPTIONS":
        return make_response("", 204)

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "파일이 없습니다"}), 400
    mode = request.form.get("mode", "analysis")

    # 원본 읽기 (속도/메모리 최적화 하이브리드)
    #  1) ezdxf.readfile(임시파일): 빠르고 메모리 적게 씀(1GB 서버 스왑 방지).
    #     $DWGCODEPAGE로 인코딩 자동결정(cp949 포함) → 한글 안 깨짐.
    #  2) recover.read: readfile 실패/빈결과일 때만. 손상파일 복구(느리지만 안전).
    #  3) 텍스트 디코드: 최후 폴백.
    raw = f.read()
    doc = None
    import tempfile as _tempfile_ngii
    import os as _os_ngii
    _tmp_ngii = None
    try:
        with _tempfile_ngii.NamedTemporaryFile(suffix='.dxf', delete=False) as _tf:
            _tf.write(raw)
            _tmp_ngii = _tf.name
        doc = ezdxf.readfile(_tmp_ngii)
        if sum(1 for _ in doc.modelspace()) == 0:
            doc = None  # 빈 결과 → 폴백
    except Exception:
        doc = None
    finally:
        if _tmp_ngii:
            try:
                _os_ngii.unlink(_tmp_ngii)
            except Exception:
                pass
    if doc is None:
        # 폴백1: recover (손상파일 복구 + 인코딩 무관)
        try:
            from ezdxf import recover
            doc, _auditor = recover.read(io.BytesIO(raw))
            if sum(1 for _ in doc.modelspace()) == 0:
                doc = None
        except Exception:
            doc = None
    if doc is None:
        # 폴백2: 텍스트 디코드 후 읽기
        for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
            try:
                doc = ezdxf.read(io.StringIO(raw.decode(enc)))
                if sum(1 for _ in doc.modelspace()) > 0:
                    break
                doc = None
            except Exception:
                continue
    if doc is None:
        return jsonify({"error": "DXF를 읽을 수 없습니다"}), 400

    src_ver = doc.dxfversion
    msp = doc.modelspace()

    all_layers = {e.dxf.layer for e in msp if e.dxf.hasattr('layer')}
    if mode == "analysis":
        target_layers = {n for n in all_layers if _ngii_is_analysis_layer(n)}
    else:
        target_layers = {n for n in all_layers if n not in ('0', 'Defpoints')}

    new = ezdxf.new('R12', setup=True)
    nmsp = new.modelspace()

    # 사용 블록 복사
    used_blocks = set()
    for e in msp:
        if e.dxftype() == 'INSERT' and e.dxf.layer in target_layers:
            used_blocks.add(e.dxf.name)
    for bname in used_blocks:
        if bname in doc.blocks and bname not in new.blocks:
            try:
                src_block = doc.blocks.get(bname)
                nb = new.blocks.new(name=bname)
                for be in src_block:
                    _ngii_copy_block_entity(be, nb)
            except Exception:
                pass

    # 엔티티 복사 + 좌표범위 누적 + 실제 사용 레이어 기록
    used_layers = set()
    copied = skipped = 0
    minx = miny = 1e18
    maxx = maxy = -1e18

    def _upd(x, y):
        nonlocal minx, miny, maxx, maxy
        if x < minx: minx = x
        if x > maxx: maxx = x
        if y < miny: miny = y
        if y > maxy: maxy = y

    for e in msp:
        lay = e.dxf.layer if e.dxf.hasattr('layer') else '0'
        if lay not in target_layers:
            continue
        t = e.dxftype()
        color = e.dxf.color if e.dxf.hasattr('color') else 256
        try:
            if t == 'LWPOLYLINE':
                pts = [(p[0], p[1]) for p in e.get_points('xy')]
                if len(pts) < 2:
                    skipped += 1; continue
                elev = float(e.dxf.elevation or 0.0)
                if elev:  # 등고선: z 보존 위해 3D 폴리라인
                    nmsp.add_polyline3d([(x, y, elev) for x, y in pts],
                                        dxfattribs={'layer': lay, 'color': color})
                else:
                    pl = nmsp.add_polyline2d(pts, dxfattribs={'layer': lay, 'color': color})
                    if e.closed:
                        pl.close(True)
                for x, y in pts: _upd(x, y)
                used_layers.add(lay); copied += 1

            elif t == 'POLYLINE':
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                zlist = [v.dxf.location.z for v in e.vertices]
                if len(pts) < 2:
                    skipped += 1; continue
                if any(abs(z) > 1e-9 for z in zlist):
                    nmsp.add_polyline3d([(x, y, z) for (x, y), z in zip(pts, zlist)],
                                        dxfattribs={'layer': lay, 'color': color})
                else:
                    pl = nmsp.add_polyline2d(pts, dxfattribs={'layer': lay, 'color': color})
                    if e.is_closed:
                        pl.close(True)
                for x, y in pts: _upd(x, y)
                used_layers.add(lay); copied += 1

            elif t == 'LINE':
                s, en = e.dxf.start, e.dxf.end
                nmsp.add_line(s, en, dxfattribs={'layer': lay, 'color': color})
                _upd(s.x, s.y); _upd(en.x, en.y)
                used_layers.add(lay); copied += 1

            elif t == 'POINT':
                p = e.dxf.location
                nmsp.add_point(p, dxfattribs={'layer': lay, 'color': color})
                _upd(p.x, p.y); used_layers.add(lay); copied += 1

            elif t == 'CIRCLE':
                c = e.dxf.center; r = e.dxf.radius
                nmsp.add_circle(c, r, dxfattribs={'layer': lay, 'color': color})
                _upd(c.x - r, c.y - r); _upd(c.x + r, c.y + r)
                used_layers.add(lay); copied += 1

            elif t == 'ARC':
                c = e.dxf.center
                nmsp.add_arc(c, e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle,
                             dxfattribs={'layer': lay, 'color': color})
                _upd(c.x, c.y); used_layers.add(lay); copied += 1

            elif t == 'TEXT':
                ip = e.dxf.insert
                nmsp.add_text(e.dxf.text,
                              dxfattribs={'layer': lay, 'color': color,
                                          'height': e.dxf.height,
                                          'insert': (ip.x, ip.y),
                                          'rotation': e.dxf.get('rotation', 0)})
                _upd(ip.x, ip.y); used_layers.add(lay); copied += 1

            elif t == 'MTEXT':
                ip = e.dxf.insert
                nmsp.add_text((e.text or '').split('\n')[0],
                              dxfattribs={'layer': lay, 'color': color,
                                          'height': e.dxf.char_height,
                                          'insert': (ip.x, ip.y)})
                _upd(ip.x, ip.y); used_layers.add(lay); copied += 1

            elif t == 'INSERT':
                ip = e.dxf.insert
                nmsp.add_blockref(e.dxf.name, (ip.x, ip.y, ip.z),
                                  dxfattribs={'layer': lay, 'color': color})
                _upd(ip.x, ip.y); used_layers.add(lay); copied += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1

    # 실제 엔티티가 들어간 레이어만 테이블 정의
    for lay in used_layers:
        if lay in new.layers:
            continue
        try:
            sl = doc.layers.get(lay)
            new.layers.add(name=lay, color=(sl.color or 7))
        except Exception:
            new.layers.add(name=lay)

    # ★ 캐드에서 열자마자 보이게: 뷰포트 설정
    if maxx > minx:
        try:
            new.set_modelspace_vport(height=(maxy - miny) * 1.05,
                                     center=((minx + maxx) / 2, (miny + maxy) / 2))
        except Exception:
            pass

    # 직렬화
    buf = io.StringIO()
    new.write(buf)
    txt = buf.getvalue()
    # EXTMIN/MAX 더미값 치환
    if maxx > minx:
        txt = _ngii_fix_extents(txt, 'EXTMIN', minx, miny)
        txt = _ngii_fix_extents(txt, 'EXTMAX', maxx, maxy)
    out_bytes = io.BytesIO(txt.encode('utf-8'))

    logger.info(f"[CONVERT] {f.filename} {src_ver}->R12 mode={mode} "
                f"layers={len(used_layers)} copied={copied} skipped={skipped}")

    base = (f.filename or "drawing").rsplit('.', 1)[0]
    suffix = "_R12" if mode == "original" else "_분석용"
    resp = make_response(send_file(
        out_bytes, mimetype='application/dxf',
        as_attachment=True, download_name=f"{base}{suffix}.dxf"))
    resp.headers['X-Src-Version'] = src_ver
    resp.headers['X-Kept-Layers'] = str(len(used_layers))
    resp.headers['X-Copied-Entities'] = str(copied)
    resp.headers['Access-Control-Expose-Headers'] = 'X-Src-Version, X-Kept-Layers, X-Copied-Entities'
    return resp


def _ngii_copy_block_entity(e, target):
    """블록 정의 내부 엔티티 복사 (R12 호환)."""
    t = e.dxftype()
    lay = e.dxf.layer if e.dxf.hasattr('layer') else '0'
    color = e.dxf.color if e.dxf.hasattr('color') else 256
    try:
        if t == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in e.get_points('xy')]
            if len(pts) < 2: return
            pl = target.add_polyline2d(pts, dxfattribs={'layer': lay, 'color': color})
            if e.closed: pl.close(True)
        elif t == 'POLYLINE':
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            if len(pts) < 2: return
            target.add_polyline2d(pts, dxfattribs={'layer': lay, 'color': color})
        elif t == 'LINE':
            target.add_line(e.dxf.start, e.dxf.end, dxfattribs={'layer': lay, 'color': color})
        elif t == 'CIRCLE':
            target.add_circle(e.dxf.center, e.dxf.radius, dxfattribs={'layer': lay, 'color': color})
        elif t == 'ARC':
            target.add_arc(e.dxf.center, e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle,
                           dxfattribs={'layer': lay, 'color': color})
        elif t == 'POINT':
            target.add_point(e.dxf.location, dxfattribs={'layer': lay, 'color': color})
        elif t == 'TEXT':
            ip = e.dxf.insert
            target.add_text(e.dxf.text, dxfattribs={'layer': lay, 'color': color,
                            'height': e.dxf.height, 'insert': (ip.x, ip.y)})
    except Exception:
        pass


# ============================================================
#  산e랑 공간정보 변환 (DXF 폴리라인 → SHP/GPX)
#  - popup_dxf.html "산e랑" 탭의 /api/sanrang/list, /api/sanrang/convert 처리
#  - 원본 데스크톱앱(산e랑_변환기_v1.1) 로직 흡수:
#    쓰레기좌표 제거(5σ) + 중복 폴리라인 제거(85% 겹침) + 면적순 정렬
#  - LWPOLYLINE + POLYLINE(2D) 둘 다 인식 (R12 변환본 호환)
#  - 디스크 출력 대신 메모리(BytesIO)에서 처리 → HTTP 응답으로 반환
#  - pyshp(shapefile)는 SHP 변환 함수 안에서 import (미설치여도 서버는 정상 기동)
# ============================================================
import zipfile as _zip_san

_SAN_PRJ = {
    "EPSG:5185": 'PROJCS["KGD2002 / West Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",125],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5186": 'PROJCS["KGD2002 / Central Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",127],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5187": 'PROJCS["KGD2002 / East Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",129],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5188": 'PROJCS["KGD2002 / East Sea Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",131],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
}


def _san_remove_junk(coords):
    """쓰레기 좌표 제거: TM 정상범위(abs>=1000)로 통계 세우고 5σ 밖 이상치 제거."""
    if len(coords) < 4:
        return coords
    pts = coords[:-1] if coords[0] == coords[-1] else list(coords)
    ref = [(x, y) for x, y in pts if abs(x) >= 1000 and abs(y) >= 1000]
    if len(ref) < 3:
        return coords
    cx = sum(x for x, y in ref) / len(ref)
    cy = sum(y for x, y in ref) / len(ref)
    sx = math.sqrt(sum((x - cx) ** 2 for x, y in ref) / len(ref)) or 1.0
    sy = math.sqrt(sum((y - cy) ** 2 for x, y in ref) / len(ref)) or 1.0
    TH = 5.0
    cleaned = [(x, y) for x, y in pts
               if abs(x - cx) <= TH * sx and abs(y - cy) <= TH * sy]
    if len(cleaned) < 3:
        return coords
    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return cleaned


def _san_make_sig(coords, precision=1):
    return frozenset(
        (round(x, precision), round(y, precision))
        for x, y in (coords[:-1] if coords[0] == coords[-1] else coords)
    )


def _san_deduplicate(polylines, precision=1):
    """중복 폴리라인 제거: 정상좌표 집합 85%+ 겹치면 유효좌표 적은 쪽 제거."""
    sigs = [_san_make_sig(p["coords"], precision) for p in polylines]
    remove = set()
    n = len(polylines)

    def valid_n(p):
        return sum(1 for x, y in p["coords"][:-1]
                   if abs(x) >= 1000 and abs(y) >= 1000)

    for i in range(n):
        if i in remove:
            continue
        for j in range(i + 1, n):
            if j in remove:
                continue
            si, sj = sigs[i], sigs[j]
            smaller = si if len(si) <= len(sj) else sj
            larger = sj if len(si) <= len(sj) else si
            overlap = len(smaller & larger) / max(len(smaller), 1)
            if overlap >= 0.85:
                if valid_n(polylines[i]) >= valid_n(polylines[j]):
                    remove.add(j)
                else:
                    remove.add(i)
                    break
    return [p for idx, p in enumerate(polylines) if idx not in remove]


def _san_shoelace(coords):
    area = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _san_read_doc(raw):
    """업로드 bytes → ezdxf 문서 (readfile 우선, recover 폴백)."""
    import tempfile
    doc, tmp = None, None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tf:
            tf.write(raw); tmp = tf.name
        doc = ezdxf.readfile(tmp)
        if sum(1 for _ in doc.modelspace()) == 0:
            doc = None
    except Exception:
        doc = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    if doc is None:
        try:
            from ezdxf import recover
            doc, _au = recover.read(io.BytesIO(raw))
            if sum(1 for _ in doc.modelspace()) == 0:
                doc = None
        except Exception:
            doc = None
    if doc is None:
        for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
            try:
                doc = ezdxf.read(io.StringIO(raw.decode(enc)))
                if sum(1 for _ in doc.modelspace()) > 0:
                    break
                doc = None
            except Exception:
                continue
    return doc


def _san_get_polylines(doc):
    """DXF → LWPOLYLINE+POLYLINE 추출 → 쓰레기제거 → 중복제거 → 면적 내림차순."""
    msp = doc.modelspace()
    result = []
    for e in msp:
        t = e.dxftype()
        if t == "LWPOLYLINE":
            raw = [(p[0], p[1]) for p in e.get_points("xy")]
        elif t == "POLYLINE":
            try:
                raw = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            except Exception:
                continue
        else:
            continue
        if len(raw) < 3:
            continue
        coords = _san_remove_junk(raw)
        if coords[0] != coords[-1]:
            coords = list(coords) + [coords[0]]
        layer = getattr(e.dxf, "layer", "0")
        result.append({
            "coords": coords,
            "area": _san_shoelace(coords),
            "n": len(coords) - 1,
            "layer": layer,
        })
    if not result:
        raise Exception("DXF에서 폴리라인을 찾을 수 없습니다.")
    result.sort(key=lambda x: x["area"], reverse=True)
    return _san_deduplicate(result)


def _san_make_shp_one(coords, epsg):
    """폴리곤 1개 → {shp,shx,dbf,prj} 바이트."""
    import shapefile
    shp_io, shx_io, dbf_io = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io,
                         shapeType=shapefile.POLYGON)
    w.field("id", "N")
    w.poly([[list(pt) for pt in coords]])
    w.record(1)
    w.close()
    return {"shp": shp_io.getvalue(), "shx": shx_io.getvalue(),
            "dbf": dbf_io.getvalue(),
            "prj": _SAN_PRJ.get(epsg, _SAN_PRJ["EPSG:5186"]).encode("utf-8")}


def _san_make_shp_zip(coords_list, epsg, merge=True):
    """폴리곤들 → SHP zip bytes.
    merge=True : 한 shp 안에 폴리곤 여러개 (공간정보.*)
    merge=False: 구역별 따로 (공간정보_1.*, 공간정보_2.* ...)
    """
    import shapefile  # pyshp (미설치 시 ImportError → 라우트에서 안내)
    zbuf = io.BytesIO()
    with _zip_san.ZipFile(zbuf, "w", _zip_san.ZIP_DEFLATED) as z:
        if merge:
            shp_io, shx_io, dbf_io = io.BytesIO(), io.BytesIO(), io.BytesIO()
            w = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io,
                                 shapeType=shapefile.POLYGON)
            w.field("id", "N")
            for i, coords in enumerate(coords_list):
                w.poly([[list(pt) for pt in coords]])
                w.record(i + 1)
            w.close()
            prj = _SAN_PRJ.get(epsg, _SAN_PRJ["EPSG:5186"]).encode("utf-8")
            z.writestr("공간정보.shp", shp_io.getvalue())
            z.writestr("공간정보.shx", shx_io.getvalue())
            z.writestr("공간정보.dbf", dbf_io.getvalue())
            z.writestr("공간정보.prj", prj)
        else:
            for i, coords in enumerate(coords_list):
                base = "공간정보" if len(coords_list) == 1 else "공간정보_%d" % (i + 1)
                parts = _san_make_shp_one(coords, epsg)
                z.writestr(base + ".shp", parts["shp"])
                z.writestr(base + ".shx", parts["shx"])
                z.writestr(base + ".dbf", parts["dbf"])
                z.writestr(base + ".prj", parts["prj"])
    return zbuf.getvalue()


def _san_make_gpx(coords_list, epsg):
    """선택 폴리곤들 → GPX(트랙 여러개, GDAL 3.10.2 형식) bytes. EPSG:4326 변환."""
    t = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
    all_latlon = []
    for coords in coords_list:
        ll = []
        for x, y in coords:
            lon, lat = t.transform(x, y)
            ll.append((lat, lon))
        all_latlon.append(ll)
    pts = [p for ll in all_latlon for p in ll]
    min_lat = min(p[0] for p in pts); max_lat = max(p[0] for p in pts)
    min_lon = min(p[1] for p in pts); max_lon = max(p[1] for p in pts)
    lines = ['<?xml version="1.0"?>']
    lines.append('<gpx version="1.1" creator="GDAL 3.10.2" '
                 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                 'xmlns:ogr="http://osgeo.org/gdal" '
                 'xmlns="http://www.topografix.com/GPX/1/1" '
                 'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
                 'http://www.topografix.com/GPX/1/1/gpx.xsd">')
    lines.append('<metadata>')
    lines.append('<bounds minlat="%.15f" minlon="%.15f" maxlat="%.15f" maxlon="%.15f"/>'
                 % (min_lat, min_lon, max_lat, max_lon))
    lines.append('</metadata>')
    for ll in all_latlon:
        lines.append('<trk>')
        lines.append('  <trkseg>')
        for lat, lon in ll:
            lines.append('    <trkpt lat="%.15f" lon="%.15f">' % (lat, lon))
            lines.append('    </trkpt>')
        lines.append('  </trkseg>')
        lines.append('</trk>')
    lines.append('</gpx>')
    return ("\n".join(lines) + "\n").encode("utf-8")


@app.route("/api/sanrang/list", methods=["POST", "OPTIONS"])
def sanrang_list():
    if request.method == "OPTIONS":
        return make_response("", 204)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "파일이 없습니다"}), 400
    try:
        doc = _san_read_doc(f.read())
        if doc is None:
            return jsonify({"error": "DXF를 읽을 수 없습니다"}), 400
        polys = _san_get_polylines(doc)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    out = [{"idx": i, "area": round(p["area"], 1), "n": p["n"], "layer": p["layer"]}
           for i, p in enumerate(polys)]
    return jsonify({"polylines": out})


def _san_make_gpx_zip(coords_list, epsg):
    """구역별 GPX 따로 → zip bytes (공간정보_1.gpx ...)."""
    zbuf = io.BytesIO()
    with _zip_san.ZipFile(zbuf, "w", _zip_san.ZIP_DEFLATED) as z:
        for i, coords in enumerate(coords_list):
            base = "공간정보" if len(coords_list) == 1 else "공간정보_%d" % (i + 1)
            z.writestr(base + ".gpx", _san_make_gpx([coords], epsg))
    return zbuf.getvalue()


def _san_selected_latlon(coords_list, epsg):
    """선택 폴리곤들 → [[[lat,lon],...], ...] (미리보기 지도용)."""
    t = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
    out = []
    for coords in coords_list:
        ring = []
        for x, y in coords:
            lon, lat = t.transform(x, y)
            ring.append([lat, lon])
        out.append(ring)
    return out


@app.route("/api/sanrang/convert", methods=["POST", "OPTIONS"])
def sanrang_convert():
    if request.method == "OPTIONS":
        return make_response("", 204)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "파일이 없습니다"}), 400
    epsg = request.form.get("epsg", "EPSG:5186")
    fmt = request.form.get("format", "shp")
    merge = request.form.get("merge", "merge") != "individual"   # 기본 합치기
    idx_raw = request.form.get("indices", "")
    try:
        indices = [int(x) for x in idx_raw.split(",") if x.strip() != ""]
    except ValueError:
        return jsonify({"error": "잘못된 선택 정보"}), 400
    if not indices:
        return jsonify({"error": "변환할 폴리라인을 선택하세요"}), 400
    try:
        doc = _san_read_doc(f.read())
        if doc is None:
            return jsonify({"error": "DXF를 읽을 수 없습니다"}), 400
        polys = _san_get_polylines(doc)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    sel = [polys[i]["coords"] for i in indices if 0 <= i < len(polys)]
    if not sel:
        return jsonify({"error": "선택한 폴리라인을 찾을 수 없습니다"}), 400
    try:
        if fmt == "gpx":
            if merge:
                data = _san_make_gpx(sel, epsg)
                resp = make_response(send_file(
                    io.BytesIO(data), mimetype="application/gpx+xml",
                    as_attachment=True, download_name="공간정보.gpx"))
            else:
                data = _san_make_gpx_zip(sel, epsg)
                resp = make_response(send_file(
                    io.BytesIO(data), mimetype="application/zip",
                    as_attachment=True, download_name="공간정보_gpx.zip"))
        else:
            data = _san_make_shp_zip(sel, epsg, merge=merge)
            resp = make_response(send_file(
                io.BytesIO(data), mimetype="application/zip",
                as_attachment=True, download_name="공간정보.zip"))
        logger.info("[SANRANG] convert fmt=%s merge=%s epsg=%s zones=%d"
                    % (fmt, merge, epsg, len(sel)))
        return resp
    except ImportError:
        return jsonify({"error": "서버에 pyshp 미설치 — 'pip install pyshp' 필요"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sanrang/preview", methods=["POST", "OPTIONS"])
def sanrang_preview():
    if request.method == "OPTIONS":
        return make_response("", 204)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "파일이 없습니다"}), 400
    epsg = request.form.get("epsg", "EPSG:5186")
    idx_raw = request.form.get("indices", "")
    try:
        indices = [int(x) for x in idx_raw.split(",") if x.strip() != ""]
    except ValueError:
        return jsonify({"error": "잘못된 선택 정보"}), 400
    try:
        doc = _san_read_doc(f.read())
        if doc is None:
            return jsonify({"error": "DXF를 읽을 수 없습니다"}), 400
        polys = _san_get_polylines(doc)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    if indices:
        sel = [polys[i]["coords"] for i in indices if 0 <= i < len(polys)]
    else:
        sel = [p["coords"] for p in polys]
    if not sel:
        return jsonify({"error": "표시할 폴리라인이 없습니다"}), 400
    return jsonify({"polygons": _san_selected_latlon(sel, epsg)})


@app.route('/favicon.ico')
def favicon():
    # 브라우저 자동요청 — 아이콘 없음(204)으로 404 콘솔에러 제거
    return ("", 204)


# === 산림청 FGIS 다운로드 테스트 (server.py의 @app.route('/api/health') 위에 붙여넣기) ===
@app.route('/api/forest-test')
def forest_test():
    import zipfile
    data_code = request.args.get('data', 'DATA016')
    doyeop    = request.args.get('doyeop', '376161')
    url = f"https://map.forest.go.kr/fgisfile/fgisData/{data_code}/doyeop/{doyeop}.zip"
    out = {"url": url}
    try:
        r = req.get(url, headers={"User-Agent":"Mozilla/5.0",
                    "Referer":"https://map.forest.go.kr/forest/"}, timeout=120)
        out["status"] = r.status_code
        out["content_type"] = r.headers.get("Content-Type","")
        body = r.content
        out["bytes"] = len(body)
        out["is_zip"] = body[:4] == b'PK\x03\x04'
        if out["is_zip"]:
            out["files"] = zipfile.ZipFile(io.BytesIO(body)).namelist()
            out["verdict"] = "OK: 로그 없이 직접 GET 으로 zip 다운로드 성공"
        else:
            out["body_head"] = body[:300].decode('utf-8','replace')
            out["verdict"] = "FAIL: zip 아님 (로그인/에러?)"
    except Exception as e:
        out["error"] = str(e); out["verdict"] = "FAIL: 요청 실패"
    return jsonify(out)

@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "message": "G-DSP 서버 실행 중", "version": "3.0", "ok": True})


# ==================== 정적 파일 + 호환 라우트 ====================
@app.route('/')
def index():
    if os.path.exists('/home/ubuntu/gdsp/index.html'):
        return send_from_directory('/home/ubuntu/gdsp', 'index.html')
    return jsonify({"service": "G-DSP", "version": "3.0", "ok": True, "message": "G-DSP 서버 실행 중"})


@app.route('/<path:filename>')
def serve_html(filename):
    if filename.endswith('.html') or filename.endswith('.css') or filename.endswith('.js'):
        return send_from_directory('/home/ubuntu/gdsp', filename)
    return jsonify({"error": "not found"}), 404


@app.route('/info')
def info():
    return jsonify({"service": "G-DSP", "version": "3.0"})


# ==== index.html이 호출하는 추가 엔드포인트들 (안 죽게 빈 응답) ====
@app.route('/api/stats/visit', methods=['POST', 'OPTIONS'])
def stats_visit():
    return jsonify({"ok": True})

@app.route('/api/stats/summary', methods=['GET'])
def stats_summary():
    return jsonify({"visits": 0, "ok": True})

@app.route('/api/iros_list', methods=['POST', 'OPTIONS'])
def iros_list():
    return jsonify({"ok": True, "data": []})

@app.route('/api/iros_owner', methods=['POST', 'OPTIONS'])
def iros_owner():
    return jsonify({"ok": True, "data": []})

@app.route('/api/iros_proxy')
def iros_proxy():
    return jsonify({"ok": True})

@app.route('/api/crawl', methods=['POST', 'OPTIONS'])
def crawl():
    return jsonify({"ok": True, "data": ""})

@app.route('/api/proxy', methods=['GET', 'POST', 'OPTIONS'])
def proxy():
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({"error": "url 필요"}), 400
        r = req.get(url, timeout=60)
        return r.content, r.status_code, dict(r.headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fetch-title', methods=['GET'])
def fetch_title():
    return jsonify({"title": ""})

@app.route('/api/ngii-tile/<int:z>/<int:x>/<int:y>')
def ngii_tile(z, x, y):
    return b'', 404

@app.route('/api/wfs-area')
def wfs_area():
    try:
        pnu = request.args.get('pnu')
        if not pnu:
            return jsonify({"error": "pnu 필요"}), 400
        params = {
            "service": "data", "request": "GetFeature",
            "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
            "format": "json", "size": "1", "attrFilter": f"pnu:=:{pnu}"
        }
        r = req.get("https://api.vworld.kr/req/data", params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        data = r.json()
        features = data.get("response", {}).get("result", {}).get("featureCollection", {}).get("features", [])
        if not features:
            return jsonify({"error": "필지 없음"}), 404
        from shapely.ops import transform as shapely_transform
        geom = shapely_shape(features[0]["geometry"])
        proj = lambda x, y: _WGS84_TO_TM5186.transform(x, y)
        geom_5186 = shapely_transform(proj, geom)
        return jsonify({
            "pnu": pnu, "area_m2": round(geom_5186.area, 2),
            "properties": features[0].get("properties", {})
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cad-box')
def cad_box():
    """화면 영역(bbox)의 필지들을 GeoJSON으로 반환 (빨간 지적선+지번 그리기용).
    파라미터: minx,miny,maxx,maxy (경위도 EPSG:4326) 또는 lng,lat(+span)"""
    try:
        minx = request.args.get('minx', type=float)
        miny = request.args.get('miny', type=float)
        maxx = request.args.get('maxx', type=float)
        maxy = request.args.get('maxy', type=float)
        # lng/lat만 온 경우 작은 박스 자동 생성
        if minx is None or maxx is None:
            lng = request.args.get('lng', type=float)
            lat = request.args.get('lat', type=float)
            if lng is None or lat is None:
                return jsonify({"error": "bbox 또는 lng/lat 필요"}), 400
            span = request.args.get('span', default=0.0025, type=float)
            minx, maxx = lng - span, lng + span
            miny, maxy = lat - span, lat + span
        size = request.args.get('size', default=200, type=int)
        # ⭐ 페이징: 화면 필지 전부 받기 (구멍처럼 보이는 누락 제거)
        all_features = []
        page = 1
        max_pages = 8  # 안전장치 (size×8)
        while page <= max_pages:
            params = {
                "service": "data", "request": "GetFeature", "version": "2.0",
                "data": "LP_PA_CBND_BUBUN", "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
                "format": "json", "size": str(size), "page": str(page),
                "geometry": "true", "attribute": "true", "crs": "EPSG:4326",
                "geomFilter": f"BOX({minx},{miny},{maxx},{maxy})"
            }
            r = req.get("https://api.vworld.kr/req/data", params=params,
                        headers={"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}, timeout=30)
            data = r.json()
            feats = data.get("response", {}).get("result", {}).get("featureCollection", {}).get("features", []) or []
            all_features.extend(feats)
            if len(feats) < size:
                break
            page += 1
        features = all_features
        out = []
        for f in features:
            props = f.get("properties", {}) or {}
            # 지번: jibun 우선, 없으면 bonbun-bubun 조합
            jibun = props.get("jibun") or ""
            if not jibun:
                bon = props.get("bonbun") or props.get("BONBUN") or ""
                bu = props.get("bubun") or props.get("BUBUN") or ""
                if bon:
                    jibun = str(int(bon)) if str(bon).isdigit() else str(bon)
                    if bu and str(bu) not in ("0", "0000", ""):
                        try: jibun += "-" + str(int(bu))
                        except: jibun += "-" + str(bu)
            out.append({
                "type": "Feature",
                "geometry": f.get("geometry"),
                "properties": {"jibun": jibun}
            })
        return jsonify({"type": "FeatureCollection", "features": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/vworld/key')
@app.route('/api/vworld-key')
def vworld_key_endpoint():
    return jsonify({"key": VWORLD_KEY})

@app.route('/api/kakao/key')
def kakao_key_endpoint():
    return jsonify({"key": KAKAO_APP_KEY})

@app.route('/api/building/key')
def building_key_endpoint():
    return jsonify({"key": BUILDING_KEY})

@app.route('/api/vworld-tile')
def vworld_tile_legacy():
    """옛날 호환 - /api/vworld-tile?z=14&x=...&y=..."""
    try:
        z = request.args.get('z')
        x = request.args.get('x')
        y = request.args.get('y')
        url = f"https://api.vworld.kr/req/wmts/1.0.0/{VWORLD_KEY}/Base/{z}/{y}/{x}.png"
        r = req.get(url, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=10)
        return r.content, 200, {'Content-Type': 'image/png'}
    except:
        return b'', 404



# ===== NEINS 환경성평가 크롤링 (server.py에 추가할 코드) =====


# ===== NEINS 환경성평가 크롤링 v2 (server.py에 추가할 코드) =====
# 흐름: 지번 → VWorld 검색 (PNU) → VWorld WFS (폴리곤) → NEINS 분석 (차트)

import re as re_module



# ===== NEINS 환경성평가 크롤링 v3 (복수 지번 지원) =====

import re as re_module



# ===== NEINS 환경성평가 크롤링 v4 (검색-선택-분석) =====

import re as re_module


def vworld_search_pnu(parcel_query, size=10):
    """VWorld 검색 API로 지번 → 후보 목록 (단일 또는 복수)"""
    url = "https://api.vworld.kr/req/search"
    params = {
        "service": "search",
        "version": "2.0",
        "request": "search",
        "size": str(size),
        "page": "1",
        "crs": "EPSG:3857",
        "format": "json",
        "type": "address",
        "category": "parcel",
        "query": parcel_query,
        "key": VWORLD_KEY,
        "domain": VWORLD_DOMAIN
    }
    headers = {"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}
    r = req.get(url, params=params, headers=headers, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    items = data.get('response', {}).get('result', {}).get('items', [])
    return items


def vworld_wfs_polygon(pnu, x, y):
    """VWorld WFS로 PNU 주변 지적도 → 해당 PNU의 폴리곤"""
    try:
        x_f = float(x)
        y_f = float(y)
    except:
        return None

    bbox = f"{x_f-300},{y_f-300},{x_f+300},{y_f+300}"
    url = "https://api.vworld.kr/req/wfs"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typename": "lp_pa_cbnd_bubun", "crs": "EPSG:3857",
        "bbox": bbox, "maxFeatures": "100", "output": "GML2",
        "domain": VWORLD_DOMAIN, "key": VWORLD_KEY
    }
    headers = {"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}
    r = req.get(url, params=params, headers=headers, timeout=20)
    if r.status_code != 200:
        return None

    xml_text = r.text
    pattern = r'<gml:featureMember>(.*?)</gml:featureMember>'
    features = re_module.findall(pattern, xml_text, re_module.DOTALL)

    for feat in features:
        if pnu in feat:
            m = re_module.search(
                r'<gml:LinearRing>.*?<gml:coordinates[^>]*>(.*?)</gml:coordinates>',
                feat, re_module.DOTALL
            )
            if m:
                return m.group(1).strip()
    return None


def coords_to_polygon_points(coords_str):
    """VWorld 좌표 → WKT 내부 형식"""
    if not coords_str:
        return None
    points = coords_str.split()
    wkt_points = []
    for p in points:
        parts = p.split(',')
        if len(parts) != 2:
            continue
        try:
            x = float(parts[0])
            y = float(parts[1])
            wkt_points.append(f"{x} {y}")
        except:
            continue
    if len(wkt_points) < 3:
        return None
    return ', '.join(wkt_points)


def neins_analyze_polygon(polygon_wkt, address):
    """NEINS 분석 API 호출 → JSON 결과"""
    session = req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    session.get("https://webgis.neins.go.kr/popup/analysisCadastralPopup.do", timeout=10)

    analyze_url = "https://webgis.neins.go.kr/proxy/proxy.do"
    params = {"url": "http://192.168.1.73:8083/rasterAnalysisFeature.do"}

    feature_type = "Polygon"  # NEINS는 항상 Polygon (다중도 파이프 구분)

    form_data = {
        "url": "http://192.168.1.73:8083/rasterAnalysisFeature.do",
        "coordinate": polygon_wkt,
        "featureType": feature_type,
        "epsgCd": "EPSG:3857",
        "scaleValue": "5000",
        "layerAnalysisGroupCd": "AG003",
        "analysisType": "cadastral",
        "dtailChkNon": "false",
        "dtailAreaChkNon": "false",
        "address": address,
        "input_url": ""
    }
    headers = {
        "Referer": "https://webgis.neins.go.kr/popup/analysisCadastralPopup.do",
        "Origin": "https://webgis.neins.go.kr",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01"
    }

    r = session.post(analyze_url, params=params, data=form_data, headers=headers, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()


@app.route('/api/vw-search', methods=['GET'])
def vworld_search_api():
    """VWorld 검색 → 후보 목록 반환"""
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({"ok": False, "error": "검색어 필요"}), 400

    try:
        items = vworld_search_pnu(query, size=10)
        if not items:
            return jsonify({"ok": False, "error": "검색 결과 없음"}), 404

        # 클라이언트 친화적 형태로 변환
        result_items = []
        for item in items:
            result_items.append({
                "pnu": item.get('id', ''),
                "parcel": item.get('address', {}).get('parcel', ''),
                "point": item.get('point', {})
            })

        return jsonify({"ok": True, "items": result_items, "total": len(result_items)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/neins-full', methods=['GET', 'POST'])
def neins_full_v4():
    """지번 → 검색 → 폴리곤 → 분석 (단수/복수)"""
    try:
        if request.method == 'POST':
            body = request.get_json() or {}
            parcels = body.get('parcels', [])
            if isinstance(parcels, str):
                parcels = [parcels]
        else:
            parcels_str = request.args.get('parcels', '').strip()
            single = request.args.get('parcel', '').strip()
            if parcels_str:
                parcels = [p.strip() for p in parcels_str.split('|||') if p.strip()]
            elif single:
                parcels = [single]
            else:
                parcels = []

        if not parcels:
            return jsonify({"ok": False, "error": "지번 필요"}), 400

        polygon_parts = []
        addrs = []
        searches = []

        for parcel in parcels:
            items = vworld_search_pnu(parcel, size=1)
            if not items:
                return jsonify({"ok": False, "error": f"VWorld 검색 실패: {parcel}"}), 404

            item = items[0]
            pnu = item.get('id', '')
            addr = item.get('address', {}).get('parcel', '')
            point = item.get('point', {})

            coords_raw = vworld_wfs_polygon(pnu, point.get('x'), point.get('y'))
            if not coords_raw:
                return jsonify({"ok": False, "error": f"WFS 폴리곤 추출 실패: {parcel}", "pnu": pnu}), 404

            poly_points = coords_to_polygon_points(coords_raw)
            if not poly_points:
                return jsonify({"ok": False, "error": f"폴리곤 변환 실패: {parcel}"}), 500

            polygon_parts.append(poly_points)
            addrs.append(addr)
            searches.append({"pnu": pnu, "parcel": addr, "point": point})

        # NEINS 방식: POLYGON|POLYGON 파이프 구분
        polygon_wkt = '|'.join(['POLYGON ((' + p + '))' for p in polygon_parts])

        full_addr = '|'.join(addrs)  # NEINS 형식
        analyze_data = neins_analyze_polygon(polygon_wkt, full_addr)
        if not analyze_data:
            return jsonify({"ok": False, "error": "NEINS 분석 실패"}), 500

        # 각 필지의 개별 폴리곤들도 반환 (미니 지도용)
        individual_polygons = ['POLYGON ((' + p + '))' for p in polygon_parts]

        return jsonify({
            "ok": True,
            "search": searches[0] if len(searches) == 1 else {
                "pnu": ' / '.join([s['pnu'] for s in searches]),
                "parcel": full_addr,
                "point": searches[0]['point'],
                "all_searches": searches
            },
            "analyze": analyze_data,
            "polygons": individual_polygons  # 개별 필지 폴리곤들
        })

    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "error": str(e),
            "trace": traceback.format_exc()[:800]
        }), 500


@app.route('/api/neins-analyze-direct', methods=['POST'])
def neins_analyze_direct_v4():
    """직접 POLYGON/MULTIPOLYGON 좌표 받아서 분석"""
    try:
        body = request.get_json() or {}
        polygon_wkt = body.get('polygon', '').strip()
        address = body.get('address', '')
        if not polygon_wkt:
            return jsonify({"ok": False, "error": "POLYGON 필요"}), 400

        result = neins_analyze_polygon(polygon_wkt, address)
        if not result:
            return jsonify({"ok": False, "error": "NEINS 분석 실패"}), 500

        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 산림청 FGIS 자료 파싱 라우트  [방11]
#   산사태 GeoTIFF(EPSG:5181) → 등급색 PNG + WGS84 bounds
#   임상/토양 Shapefile(EPSG:5179) → WGS84 GeoJSON + 속성
# ============================================================
_FGIS_T_5181 = Transformer.from_crs("EPSG:5181", "EPSG:4326", always_xy=True)
_FGIS_T_5179 = Transformer.from_crs("EPSG:5179", "EPSG:4326", always_xy=True)
_FGIS_CLR = {1:(255,0,0),2:(255,201,0),3:(182,255,142),4:(48,194,255),5:(0,0,255)}

def _fgis_detect(names):
    low=[n.lower() for n in names]
    if any(n.endswith('.tif') for n in low) and any(n.endswith('.tfw') for n in low): return 'sansatae'
    if any(n.endswith('.shp') for n in low): return 'shapefile'
    return None

def _fgis_classify(field_names):
    f=set(field_names)
    if {'FRTP_NM','KOFTR_NM'} & f or 'KOFTR_GROU' in f: return 'imsang'
    if {'SLDPT_TPCD','PRRCK_LARG'} & f: return 'toyang'
    return 'unknown'

def _fgis_safe(v):
    if isinstance(v, bytes):
        try: return v.decode('cp949').strip()
        except: return v.decode('utf-8','ignore').strip()
    return v

def _fgis_reproj(geom, T):
    gtype=geom['type']; coords=geom['coordinates']
    def pt(xy):
        lng,lat=T.transform(xy[0],xy[1]); return [lng,lat]
    def ring(r): return [pt(c) for c in r]
    if gtype=='Point': new=pt(coords)
    elif gtype in ('LineString','MultiPoint'): new=ring(coords)
    elif gtype in ('Polygon','MultiLineString'): new=[ring(r) for r in coords]
    elif gtype=='MultiPolygon': new=[[ring(r) for r in poly] for poly in coords]
    else: new=coords
    return {'type':gtype,'coordinates':new}

def _fgis_parse_tif(tif_bytes, tfw_text):
    from PIL import Image
    import numpy as np
    v=[float(x) for x in tfw_text.split()]
    pxW,_,_,pxH,x0,y0=v
    # tfw C/F(x0,y0)=좌상단 픽셀 "중심" → 이미지 모서리로 반픽셀 보정(산사태 정합 오차 수정)
    x0 -= pxW/2.0; y0 -= pxH/2.0
    img=Image.open(io.BytesIO(tif_bytes)); arr=np.array(img)
    if arr.ndim==3: arr=arr[:,:,0]
    H,W=arr.shape
    rgba=np.zeros((H,W,4),dtype=np.uint8); gc={}
    for g,(r,gg,b) in _FGIS_CLR.items():
        m=(arr==g); c=int(m.sum())
        if c: rgba[m]=[r,gg,b,200]; gc[g]=c
    out=Image.fromarray(rgba,'RGBA'); buf=io.BytesIO(); out.save(buf,format='PNG')
    b64=base64.b64encode(buf.getvalue()).decode('ascii')
    x1=x0+W*pxW; y1=y0+H*pxH
    sw=_FGIS_T_5181.transform(min(x0,x1),min(y0,y1))
    ne=_FGIS_T_5181.transform(max(x0,x1),max(y0,y1))
    return {'kind':'sansatae','png_base64':b64,
            'bounds':[[sw[1],sw[0]],[ne[1],ne[0]]],
            'width':W,'height':H,'grade_counts':gc,
            'legend':{str(k):list(v) for k,v in _FGIS_CLR.items()}}

def _fgis_parse_shp(shp_path):
    import shapefile
    sf=shapefile.Reader(shp_path, encoding='cp949')
    fns=[f[0] for f in sf.fields[1:]]
    kind=_fgis_classify(fns)
    shapes=list(sf.iterShapeRecords())
    # 첫 도형의 좌표 1개 (CRS 판별 샘플)
    def _firstpt(c):
        while isinstance(c,(list,tuple)) and c and isinstance(c[0],(list,tuple)): c=c[0]
        return c
    sample=None
    for sr in shapes:
        fp=_firstpt(sr.shape.__geo_interface__.get('coordinates'))
        if fp and len(fp)>=2: sample=(fp[0],fp[1]); break
    def _in_korea(lng,lat): return 124.0<=lng<=132.0 and 33.0<=lat<=43.5
    T=None; crs_used=''
    # 1) .prj 읽어 판별 → 결과가 한국 범위면 채택
    try:
        prj=shp_path[:-4]+'.prj' if shp_path.lower().endswith('.shp') else shp_path+'.prj'
        if os.path.exists(prj):
            from pyproj import CRS
            wkt=open(prj, encoding='utf-8', errors='ignore').read().strip()
            if wkt:
                c=CRS.from_wkt(wkt); T2=Transformer.from_crs(c, "EPSG:4326", always_xy=True)
                if sample:
                    ll=T2.transform(sample[0], sample[1])
                    if _in_korea(ll[0], ll[1]): T=T2; crs_used=('EPSG:%s'%c.to_epsg()) if c.to_epsg() else 'prj'
                else: T=T2; crs_used='prj'
    except Exception:
        T=None
    # 2) .prj 없음/실패/한국밖 → 후보 좌표계 자동판별 (샘플이 한국에 들어오는 것)
    if T is None and sample:
        for epsg in (5186,5174,5179,5187,5185,5182,4326):
            try:
                tt=Transformer.from_crs("EPSG:%d"%epsg, "EPSG:4326", always_xy=True)
                ll=tt.transform(sample[0], sample[1])
                if _in_korea(ll[0], ll[1]): T=tt; crs_used='EPSG:%d(auto)'%epsg; break
            except Exception:
                pass
    if T is None: T=_FGIS_T_5179; crs_used='5179(fallback)'
    feats=[]
    for sr in shapes:
        geom=_fgis_reproj(sr.shape.__geo_interface__, T)
        props={k:_fgis_safe(val) for k,val in zip(fns, list(sr.record))}
        feats.append({'type':'Feature','geometry':geom,'properties':props})
    return {'kind':kind,'geojson':{'type':'FeatureCollection','features':feats},
            'count':len(feats),'fields':fns,'crs':crs_used}

@app.route('/api/forest-parse', methods=['POST','OPTIONS'])
def forest_parse():
    """산림청 FGIS zip 업로드 → 종류 자동판별 → 지도용 데이터(JSON)."""
    if request.method=='OPTIONS': return '', 200
    import tempfile, shutil
    results, errors = [], []
    files=list(request.files.values())
    if not files:
        return jsonify({'ok':False,'msg':'zip 파일을 업로드하세요'}), 400
    for fs in files:
        fname=fs.filename or 'upload.zip'
        try:
            data=fs.read()
            zf=_zip_san.ZipFile(io.BytesIO(data))
            names=zf.namelist()
            typ=_fgis_detect(names)
            if typ=='sansatae':
                tif=zf.read(next(n for n in names if n.lower().endswith('.tif')))
                tfw=zf.read(next(n for n in names if n.lower().endswith('.tfw'))).decode('ascii','ignore')
                r=_fgis_parse_tif(tif,tfw); r['source_file']=fname; results.append(r)
            elif typ=='shapefile':
                tmp=tempfile.mkdtemp(prefix='fgis_'); base=None
                try:
                    for n in names:
                        ln=n.lower()
                        if ln.endswith(('.shp','.dbf','.shx','.prj','.cpg')):
                            op=os.path.join(tmp, os.path.basename(n))
                            with open(op,'wb') as w: w.write(zf.read(n))
                            if ln.endswith('.shp'): base=op
                    if not base:
                        errors.append(f'{fname}: .shp 없음'); continue
                    r=_fgis_parse_shp(base); r['source_file']=fname; results.append(r)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            else:
                errors.append(f'{fname}: 산사태(tif)/임상·토양(shp) 아님 — {names[:3]}')
        except Exception as e:
            errors.append(f'{fname}: {e}')
    return jsonify({'ok':len(results)>0,'results':results,'errors':errors})



# ============================================================
# 국토정보플랫폼 항공사진 프록시 라우트  [방11]
#   연도별 항공사진 WMTS 타일 (키 숨김 + CORS 우회)
#   엔드포인트: map.ngii.go.kr/airmapprime/map/wmts
#   TileMatrixSet=NGIS_AIR (EPSG:5179, origin -200000/4000000), 레이어 mapprime:air_{연도}
#   연도: 2011~2024 (전국 공통)
# ============================================================
NGII_KEY = "1FF267EF92D38E0F472E32DB6DB7A1A5B92F185F1D"
NGII_AIR_WMTS = "https://map.ngii.go.kr/airmapprime/map/wmts"
NGII_YEAR_API = "https://map.ngii.go.kr/openapi/AirPhotoYearList.do"

# 항공 WMTS 타일 격자 (GetCapabilities NGIS_AIR 기준)
_AIR_ORIGIN = (-200000.0, 4000000.0)
_AIR_RES = [66846.72,33423.36,16711.68,8355.84,4177.92,2088.96,1044.48,522.24,
            261.12,130.56,65.28,32.64,16.32,8.16,4.08,2.04,1.02,0.51,0.255,0.1275,0.06375]
_AIR_TILE = 256
_WGS84_TO_5179 = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)

def _air_lnglat_to_tile(lng, lat, z):
    """WGS84 → NGIS_AIR 타일 좌표 (col,row) + 타일 내 픽셀 위치."""
    x, y = _WGS84_TO_5179.transform(lng, lat)
    span = _AIR_TILE * _AIR_RES[z]
    col = (x - _AIR_ORIGIN[0]) / span
    row = (_AIR_ORIGIN[1] - y) / span
    return col, row  # float (정수부=타일번호, 소수부=타일내 위치)

@app.route('/api/air-years')
def air_years():
    """항공사진 가능 연도 목록."""
    try:
        r = req.get(NGII_YEAR_API, params={"apikey": NGII_KEY}, timeout=12)
        data = r.json()
        years = [it["year"] for it in data.get("RESULT", [])]
        return jsonify({"ok": True, "years": years})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "years": list(range(2011, 2025))}), 200

def _air_fetch_tile(year, z, col, row):
    """단일 항공 타일 bytes (없으면 None)."""
    params = {
        "SERVICE": "WMTS", "VERSION": "1.0.0", "REQUEST": "GetTile",
        "LAYER": f"mapprime:air_{year}", "STYLE": "",
        "TILEMATRIXSET": "NGIS_AIR", "TILEMATRIX": str(z),
        "TILEROW": str(int(row)), "TILECOL": str(int(col)),
        "FORMAT": "image/jpeg", "apikey": NGII_KEY,
    }
    r = req.get(NGII_AIR_WMTS, params=params, timeout=15)
    ct = r.headers.get('Content-Type', '')
    if r.status_code == 200 and 'image' in ct:
        return r.content
    return None

@app.route('/api/air-photo')
def air_photo():
    """허가지 위치의 연도별 항공사진 1장 (지정 크기, 중심좌표 기준).
    params: lng, lat (WGS84 중심), year, z(줌, 기본15), size(출력 px, 기본512)
    중심 주변 타일들을 모자이크해서 size×size로 잘라 PNG 반환.
    """
    from PIL import Image
    try:
        lng = float(request.args['lng']); lat = float(request.args['lat'])
        year = int(request.args['year'])
        z = int(request.args.get('z', 15))
        size = int(request.args.get('size', 512))
        z = max(10, min(z, 18))

        colf, rowf = _air_lnglat_to_tile(lng, lat, z)
        c0, r0 = int(colf), int(rowf)
        # 중심이 size를 덮도록 충분한 타일 범위 (size/256 + 여유)
        nt = size // _AIR_TILE + 2
        half = nt // 2 + 1
        canvas = Image.new('RGB', ((2*half+1)*_AIR_TILE, (2*half+1)*_AIR_TILE), (40,40,40))
        got = 0
        for dc in range(-half, half+1):
            for dr in range(-half, half+1):
                tb = _air_fetch_tile(year, z, c0+dc, r0+dr)
                if tb:
                    try:
                        tile = Image.open(io.BytesIO(tb)).convert('RGB')
                        canvas.paste(tile, ((dc+half)*_AIR_TILE, (dr+half)*_AIR_TILE))
                        got += 1
                    except Exception:
                        pass
        if got == 0:
            return jsonify({"ok": False, "msg": f"{year}년 항공사진 없음(이 위치)"}), 404
        # 중심 픽셀 = (colf - c0 + half)*256 , (rowf - r0 + half)*256
        cx = (colf - c0 + half) * _AIR_TILE
        cy = (rowf - r0 + half) * _AIR_TILE
        left = int(cx - size/2); top = int(cy - size/2)
        left = max(0, min(left, canvas.width - size))
        top = max(0, min(top, canvas.height - size))
        crop = canvas.crop((left, top, left+size, top+size))
        buf = io.BytesIO(); crop.save(buf, format='PNG')
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'image/png'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        logger.error(f"[air-photo] {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500


# ═══ KIGAM 지질도 모암 자동조회 ═══
KIGAM_WMS = "https://data.kigam.re.kr/mgeo/geoserver/wms"
KIGAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.kigam.re.kr/map/",
    "Accept": "application/json",
}

@app.route('/api/kigam-moam')
def kigam_moam():
    try:
        lat = float(request.args.get('lat', ''))
        lng = float(request.args.get('lng', ''))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'msg': '좌표(lat,lng) 필요'}), 400
    d = 0.005
    bbox = "%f,%f,%f,%f" % (lng-d, lat-d, lng+d, lat+d)
    W, H = 256, 256
    params = {
        'service': 'WMS', 'version': '1.1.1', 'request': 'GetFeatureInfo',
        'layers': 'geoOpen:L_50K_Geology_Map',
        'query_layers': 'geoOpen:L_50K_Geology_Map',
        'srs': 'EPSG:4326', 'bbox': bbox,
        'width': W, 'height': H, 'x': W//2, 'y': H//2,
        'info_format': 'application/json', 'feature_count': 5,
    }
    try:
        r = req.get(KIGAM_WMS, params=params, headers=KIGAM_HEADERS, timeout=10)
        if r.status_code != 200:
            return jsonify({'ok': False, 'msg': 'KIGAM 응답 %d' % r.status_code}), 502
        data = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'msg': 'KIGAM 실패: %s' % e}), 502
    geo_feat = None
    for f in data.get('features', []):
        if '대표암상' in f.get('properties', {}):
            geo_feat = f
            break
    if not geo_feat:
        return jsonify({'ok': False, 'msg': '이 지점에 지질 정보 없음'})
    p = geo_feat.get('properties', {})
    dopok = p.get('도폭', '')
    if '<' in dopok:
        dopok = dopok.split('<')[0].strip()
    return jsonify({
        'ok': True, '시대': p.get('시대', ''), '지층': p.get('지층', ''),
        '대표암상': p.get('대표암상', ''), '기호': p.get('기호', ''),
        '도폭': dopok, 'symnum': p.get('symnum', ''),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
