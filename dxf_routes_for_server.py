# =============================================================
# server.py 추가 코드 — DXF 변환 통합 (R12 + 산e랑)
# =============================================================
# 적용 방법:
#   1. server.py 상단 import 영역에 추가:
#        import re, math, tempfile, zipfile
#        try:
#            import shapefile  # pip install pyshp
#            HAS_PYSHP = True
#        except ImportError:
#            HAS_PYSHP = False
#
#   2. 의존성 설치 (Oracle 서버):
#        pip install pyshp --break-system-packages
#        # ezdxf, pyproj는 이미 설치되어 있음
#
#   3. 아래 코드 전체를 server.py에 추가 (다른 라우트들과 같은 위치)
#
#   4. 서버 재시작:
#        sudo pkill -9 -f gunicorn
#        sudo systemctl restart gdsp
# =============================================================

import re as _re_dxf
import math as _math_dxf
import tempfile as _tmp_dxf
import zipfile as _zip_dxf

try:
    import shapefile as _shapefile
    HAS_PYSHP = True
except ImportError:
    HAS_PYSHP = False


# =============================================================
# [1] 수치지형도 R12 변환 + 분석용 필터링
# =============================================================
ANALYSIS_LAYER_PATTERNS = [
    _re_dxf.compile(r'^B0014\d{3}$'),   # 등고선
    _re_dxf.compile(r'^F0017\d{3}$'),   # 표고점
    _re_dxf.compile(r'^C0076\d{3}$'),   # 하천
    _re_dxf.compile(r'^C0052\d{3}$'),   # 수계/호안
]

def _layer_kept(name):
    if not name or name in ('0', 'Defpoints'):
        return False
    return any(p.match(name) for p in ANALYSIS_LAYER_PATTERNS)


def _copy_entity_r12(e, target):
    """수치지형도용 엔티티 복사 (R12 호환)"""
    t = e.dxftype()
    layer = e.dxf.get('layer', '0')
    color = e.dxf.get('color', 256)
    base = {'layer': layer, 'color': color}

    try:
        if t == 'LWPOLYLINE':
            pts = [(p[0], p[1]) for p in e.get_points()]
            if len(pts) >= 2:
                pl = target.add_polyline2d(pts, dxfattribs=base)
                if e.closed:
                    try: pl.close(True)
                    except Exception: pass
                return 1

        elif t == 'POLYLINE':
            pts = [(v.dxf.location[0], v.dxf.location[1]) for v in e.vertices]
            if len(pts) >= 2:
                target.add_polyline2d(pts, dxfattribs=base)
                return 1

        elif t == 'LINE':
            target.add_line(start=tuple(e.dxf.start)[:2], end=tuple(e.dxf.end)[:2], dxfattribs=base)
            return 1

        elif t == 'POINT':
            target.add_point(e.dxf.location, dxfattribs=base)
            return 1

        elif t == 'CIRCLE':
            target.add_circle(e.dxf.center, e.dxf.radius, dxfattribs=base)
            return 1

        elif t == 'ARC':
            target.add_arc(center=e.dxf.center, radius=e.dxf.radius,
                start_angle=e.dxf.start_angle, end_angle=e.dxf.end_angle, dxfattribs=base)
            return 1

        elif t == 'TEXT':
            txt = target.add_text(e.dxf.text, dxfattribs={
                **base, 'height': e.dxf.get('height', 1), 'rotation': e.dxf.get('rotation', 0)
            })
            try: txt.dxf.insert = e.dxf.insert
            except Exception: pass
            return 1

        elif t == 'MTEXT':
            try:
                txt = target.add_text(e.text, dxfattribs={
                    **base, 'height': e.dxf.get('char_height', 1), 'rotation': e.dxf.get('rotation', 0)
                })
                txt.dxf.insert = e.dxf.insert
                return 1
            except Exception:
                pass

        elif t == 'INSERT':
            target.add_blockref(e.dxf.name, e.dxf.get('insert', (0, 0, 0)), dxfattribs={
                **base, 'xscale': e.dxf.get('xscale', 1), 'yscale': e.dxf.get('yscale', 1),
                'rotation': e.dxf.get('rotation', 0)
            })
            return 1
    except Exception:
        return 0
    return 0


@app.route('/api/convert-dxf', methods=['POST'])
def api_convert_dxf():
    """수치지형도 DXF → R12 변환 (mode=original/analysis)"""
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.dxf'):
        return jsonify({'error': 'DXF 파일만 가능합니다'}), 400

    mode = request.form.get('mode', 'original').strip().lower()
    if mode not in ('original', 'analysis'):
        mode = 'original'

    tmp_path = None
    try:
        with _tmp_dxf.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        # 원본 읽기
        try:
            src = ezdxf.readfile(tmp_path, encoding='cp949')
        except Exception:
            src = ezdxf.readfile(tmp_path)

        # 새 R12 문서
        new = ezdxf.new('R12')
        new_msp = new.modelspace()

        # 레이어 복사
        kept = set()
        for layer in src.layers:
            name = layer.dxf.name
            if name in ('0', 'Defpoints'):
                continue
            if mode == 'analysis' and not _layer_kept(name):
                continue
            try:
                new.layers.new(name, dxfattribs={'color': layer.dxf.get('color', 7)})
                kept.add(name)
            except Exception:
                pass

        # 블록 복사
        for bdef in src.blocks:
            if bdef.name.startswith('*'):
                continue
            try:
                nb = new.blocks.new(bdef.name)
                for e in bdef:
                    _copy_entity_r12(e, nb)
            except Exception:
                pass

        # 모델스페이스 복사
        copied = 0
        for e in src.modelspace():
            layer = e.dxf.get('layer', '0')
            if mode == 'analysis' and layer not in kept:
                continue
            n = _copy_entity_r12(e, new_msp)
            if n: copied += n

        # 헤더 복사
        for k in ('$EXTMIN', '$EXTMAX', '$LIMMIN', '$LIMMAX'):
            try: new.header[k] = src.header[k]
            except Exception: pass

        # bytes 변환
        buf = io.StringIO()
        new.write(buf, fmt='asc')
        out_bytes = buf.getvalue().encode('cp949', errors='replace')

        # 파일명
        base_name = os.path.splitext(os.path.basename(f.filename))[0]
        suffix = '_R12' if mode == 'original' else '_분석용'
        out_name = f"{base_name}{suffix}.dxf"

        resp = send_file(io.BytesIO(out_bytes), as_attachment=True,
                         download_name=out_name, mimetype='application/octet-stream')
        resp.headers['X-Convert-Mode'] = mode
        resp.headers['X-Src-Version'] = str(src.dxfversion)
        resp.headers['X-Kept-Layers'] = str(len(kept))
        resp.headers['X-Copied-Entities'] = str(copied)
        resp.headers['Access-Control-Expose-Headers'] = 'X-Convert-Mode, X-Src-Version, X-Kept-Layers, X-Copied-Entities'
        return resp

    except Exception as e:
        import traceback
        logger.error(f"[CONVERT-DXF] {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass


# =============================================================
# [2] 산e랑 변환 — DXF LWPOLYLINE → SHP/GPX
# =============================================================
PRJ_TEXT_KO = {
    "EPSG:5185": 'PROJCS["KGD2002 / West Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",125],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5186": 'PROJCS["KGD2002 / Central Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",127],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5187": 'PROJCS["KGD2002 / East Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",129],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
    "EPSG:5188": 'PROJCS["KGD2002 / East Sea Belt 2010",GEOGCS["GCS_KGD2002",DATUM["Korean_Geodetic_Datum_2002",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",131],PARAMETER["scale_factor",1],PARAMETER["false_easting",200000],PARAMETER["false_northing",600000],UNIT["metre",1]]',
}


def _sr_remove_junk(coords):
    """5σ 이상치 제거"""
    if len(coords) < 4:
        return coords
    pts = coords[:-1] if coords[0] == coords[-1] else list(coords)
    ref = [(x, y) for x, y in pts if abs(x) >= 1000 and abs(y) >= 1000]
    if len(ref) < 3:
        return coords
    cx = sum(x for x, y in ref) / len(ref)
    cy = sum(y for x, y in ref) / len(ref)
    sx = _math_dxf.sqrt(sum((x - cx) ** 2 for x, y in ref) / len(ref)) or 1.0
    sy = _math_dxf.sqrt(sum((y - cy) ** 2 for x, y in ref) / len(ref)) or 1.0
    cleaned = [(x, y) for x, y in pts if abs(x - cx) <= 5.0 * sx and abs(y - cy) <= 5.0 * sy]
    if len(cleaned) < 3:
        return coords
    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return cleaned


def _sr_dedup(polylines, precision=1):
    """중복 폴리라인 제거 (85% 겹침)"""
    def make_sig(coords):
        return frozenset(
            (round(x, precision), round(y, precision))
            for x, y in (coords[:-1] if coords[0] == coords[-1] else coords)
        )
    sigs = [make_sig(p["coords"]) for p in polylines]
    remove = set()
    n = len(polylines)
    for i in range(n):
        if i in remove: continue
        for j in range(i + 1, n):
            if j in remove: continue
            si, sj = sigs[i], sigs[j]
            smaller = si if len(si) <= len(sj) else sj
            larger  = sj if len(si) <= len(sj) else si
            overlap = len(smaller & larger) / max(len(smaller), 1)
            if overlap >= 0.85:
                def valid_n(p):
                    return sum(1 for x, y in p["coords"][:-1] if abs(x) >= 1000 and abs(y) >= 1000)
                if valid_n(polylines[i]) >= valid_n(polylines[j]):
                    remove.add(j)
                else:
                    remove.add(i)
                    break
    return [p for idx, p in enumerate(polylines) if idx not in remove]


def _sr_get_polylines(path):
    """DXF에서 폴리라인 추출"""
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    result = []
    for e in msp:
        if e.dxftype() != "LWPOLYLINE":
            continue
        raw = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(raw) < 3:
            continue
        coords = _sr_remove_junk(raw)
        if coords[0] != coords[-1]:
            coords = list(coords) + [coords[0]]
        # 면적 계산
        area = 0.0
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            area += x1 * y2 - x2 * y1
        area = abs(area) / 2.0
        layer = getattr(e.dxf, "layer", "0")
        result.append({
            "coords": coords,
            "area":   area,
            "n":      len(coords) - 1,
            "layer":  layer,
        })
    if not result:
        raise Exception("DXF에서 폴리라인을 찾을 수 없습니다")
    result.sort(key=lambda x: x["area"], reverse=True)
    return _sr_dedup(result)


@app.route('/api/sanrang/list', methods=['POST'])
def api_sanrang_list():
    """DXF 업로드 → 폴리라인 목록 (선택 UI용)"""
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.dxf'):
        return jsonify({'error': 'DXF 파일만 가능합니다'}), 400

    tmp_path = None
    try:
        with _tmp_dxf.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        polylines = _sr_get_polylines(tmp_path)
        items = []
        for i, p in enumerate(polylines):
            items.append({
                'idx':    i,
                'area':   round(p['area'], 2),
                'n':      p['n'],
                'layer':  p['layer'],
            })
        return jsonify({'polylines': items, 'total': len(items)})

    except Exception as e:
        import traceback
        logger.error(f"[SANRANG-LIST] {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass


@app.route('/api/sanrang/convert', methods=['POST'])
def api_sanrang_convert():
    """
    DXF → SHP/GPX 변환
    - file: DXF
    - epsg: EPSG:5185/5186/5187/5188 (기본 5186)
    - format: shp / gpx (기본 shp)
    - indices: 변환할 폴리라인 인덱스 (콤마 구분, 빈 값이면 전체)
    """
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.dxf'):
        return jsonify({'error': 'DXF 파일만 가능합니다'}), 400

    epsg = request.form.get('epsg', 'EPSG:5186').strip()
    if epsg not in PRJ_TEXT_KO:
        epsg = 'EPSG:5186'
    fmt = request.form.get('format', 'shp').strip().lower()
    if fmt not in ('shp', 'gpx'):
        fmt = 'shp'
    indices_str = request.form.get('indices', '').strip()

    tmp_path = None
    try:
        with _tmp_dxf.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        polylines = _sr_get_polylines(tmp_path)
        
        # 선택된 인덱스만 필터링
        if indices_str:
            indices = [int(x) for x in indices_str.split(',') if x.strip().isdigit()]
            selected = [polylines[i] for i in indices if 0 <= i < len(polylines)]
        else:
            selected = polylines
        
        if not selected:
            return jsonify({'error': '변환할 폴리라인이 없습니다'}), 400

        coords_list = [p['coords'] for p in selected]
        base_filename = os.path.splitext(os.path.basename(f.filename))[0] + '_변환'

        if fmt == 'shp':
            if not HAS_PYSHP:
                return jsonify({'error': 'pyshp 미설치 (서버 관리자에게 문의)'}), 500
            
            with _tmp_dxf.TemporaryDirectory() as tmpdir:
                base = os.path.join(tmpdir, "polygon")
                w = _shapefile.Writer(base, shapeType=_shapefile.POLYGON)
                w.field("id", "N")
                for i, coords in enumerate(coords_list, 1):
                    w.poly([coords])
                    w.record(i)
                w.close()
                with open(base + ".prj", "w") as fp:
                    fp.write(PRJ_TEXT_KO[epsg])
                
                zip_buf = io.BytesIO()
                with _zip_dxf.ZipFile(zip_buf, "w", _zip_dxf.ZIP_DEFLATED) as z:
                    for ext in ["shp", "shx", "dbf", "prj"]:
                        z.write(base + "." + ext, arcname=f"{base_filename}.{ext}")
                zip_buf.seek(0)
                
                resp = send_file(zip_buf, as_attachment=True,
                    download_name=f"{base_filename}.zip", mimetype='application/zip')
                resp.headers['X-Polylines'] = str(len(selected))
                resp.headers['X-EPSG'] = epsg
                resp.headers['Access-Control-Expose-Headers'] = 'X-Polylines, X-EPSG'
                return resp

        else:  # gpx
            t = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
            all_latlons = []
            for coords in coords_list:
                latlons = []
                for x, y in coords:
                    lon, lat = t.transform(x, y)
                    latlons.append((lat, lon))
                all_latlons.append(latlons)
            
            all_pts = [pt for ll in all_latlons for pt in ll]
            min_lat = min(p[0] for p in all_pts)
            max_lat = max(p[0] for p in all_pts)
            min_lon = min(p[1] for p in all_pts)
            max_lon = max(p[1] for p in all_pts)
            
            lines = ['<?xml version="1.0"?>',
                '<gpx version="1.1" creator="GDAL 3.10.2" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xmlns:ogr="http://osgeo.org/gdal" '
                'xmlns="http://www.topografix.com/GPX/1/1" '
                'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
                'http://www.topografix.com/GPX/1/1/gpx.xsd">',
                '<metadata>',
                f'<bounds minlat="{min_lat:.15f}" minlon="{min_lon:.15f}" '
                f'maxlat="{max_lat:.15f}" maxlon="{max_lon:.15f}"/>',
                '</metadata>']
            for latlons in all_latlons:
                lines.append('<trk>')
                lines.append('  <trkseg>')
                for lat, lon in latlons:
                    lines.append(f'    <trkpt lat="{lat:.15f}" lon="{lon:.15f}">')
                    lines.append('    </trkpt>')
                lines.append('  </trkseg>')
                lines.append('</trk>')
            lines.append('</gpx>')
            
            gpx_bytes = ("\n".join(lines) + "\n").encode('utf-8')
            
            resp = send_file(io.BytesIO(gpx_bytes), as_attachment=True,
                download_name=f"{base_filename}.gpx", mimetype='application/gpx+xml')
            resp.headers['X-Polylines'] = str(len(selected))
            resp.headers['X-EPSG'] = epsg
            resp.headers['Access-Control-Expose-Headers'] = 'X-Polylines, X-EPSG'
            return resp

    except Exception as e:
        import traceback
        logger.error(f"[SANRANG-CONVERT] {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass
