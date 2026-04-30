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
import requests as req, ezdxf, io, logging, math, json, os, time
import xml.etree.ElementTree as ET
from ezdxf.enums import TextEntityAlignment
from pyproj import Transformer
from shapely.geometry import Point, box as Box, shape as shapely_shape, mapping as shapely_mapping
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

app = Flask(__name__)
CORS(app, supports_credentials=True)
cache = Cache(app, config={'CACHE_TYPE': 'simple', 'CACHE_DEFAULT_TIMEOUT': 3600})
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VWORLD_KEY = "16B90D39-90BB-3197-987A-54983A46F250"
VWORLD_DOMAIN = "168-107-15-68.nip.io"
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
    """일반 VWorld data API 호출"""
    url = "https://api.vworld.kr/req/data"
    params = {
        "service": "data", "request": "GetFeature", "data": vworld_data,
        "key": VWORLD_KEY, "format": "json", "size": str(size),
        "domain": VWORLD_DOMAIN,
        "geomFilter": f"BOX({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]})",
    }
    headers = {"Referer": f"https://{VWORLD_DOMAIN}", "User-Agent": "Mozilla/5.0"}
    try:
        r = req.get(url, params=params, headers=headers, timeout=25)
        data = r.json()
        fc = data.get("response", {}).get("result", {}).get("featureCollection", {})
        if fc:
            return fc.get("features", []) or []
    except Exception as e:
        logger.error(f"[WFS {vworld_data}] {e}")
    return []


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
    radius_m = max(100, min(2000, radius_m))
    requested = body.get("layers") or ["cadastra"]
    layer_ids = [l for l in requested if l in VWORLD_LAYERS]
    if not layer_ids:
        return jsonify({"error": "유효한 레이어 없음"}), 400
    jibun = (body.get("jibun") or "").strip()

    # bbox 계산 (도(deg) 단위)
    buffer_m = max(300, min(800, radius_m * 0.8))
    query_radius_m = radius_m + buffer_m
    lat_deg = query_radius_m / 111000.0
    lng_deg = query_radius_m / (111000.0 * max(0.1, math.cos(math.radians(center_lat))))
    bbox = [center_lng - lng_deg, center_lat - lat_deg, center_lng + lng_deg, center_lat + lat_deg]
    
    radius_deg = radius_m / 111000.0
    circle_filter = Point(center_lng, center_lat).buffer(radius_deg * 1.15)

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

    # 원 영역 필터링 (지적/건물은 폴리곤이라 부분교차 OK)
    for lid in layer_ids:
        filtered = []
        for f in features_by_layer.get(lid, []):
            try:
                g = shapely_shape(f.get('geometry'))
                if g.intersects(circle_filter):
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

    # 기준점
    cx, cy = _wgs84_to_cad(center_lng, center_lat)
    if "기준점" not in doc.layers:
        doc.layers.new(name="기준점", dxfattribs={"color": 8})
    try:
        msp.add_circle((cx, cy), radius=2.0, dxfattribs={"layer": "기준점"})
        note = f"G-DSP {jibun or ''} r={int(radius_m)}m"
        t = msp.add_text(note, dxfattribs={"layer": "기준점", "height": 5.0, "style": "HANGUL"})
        t.set_placement((cx, cy - radius_m - 30), align=TextEntityAlignment.MIDDLE_CENTER)
    except:
        pass

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
            "format": "json", "numOfRows": "1", "pageNo": "1", "pnu": pnu
        }
        r = req.get(url, params=params, headers={"Referer": f"https://{VWORLD_DOMAIN}"}, timeout=60)
        return jsonify(r.json())
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

@app.route('/api/vworld/key')
@app.route('/api/vworld-key')
def vworld_key_endpoint():
    return jsonify({"key": VWORLD_KEY})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
