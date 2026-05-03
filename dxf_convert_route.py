# =============================================
# server.py 에 추가할 DXF 변환 엔드포인트 (R12 + 레이어 필터)
# =============================================
# server.py 상단 import 영역에 추가:
#   import io, tempfile, os, re, ezdxf
#
# 그리고 아래 코드를 server.py 어딘가 (다른 라우트들과 같은 위치)에 붙여넣기.
# 의존성: pip install ezdxf --break-system-packages

import io, os, re, tempfile
import ezdxf
from flask import request, send_file, jsonify

# =============================================
# 분석용(경사도+수계) 레이어 화이트리스트
# 국토지리정보원 수치지형도 코드 체계 기준
# =============================================
ANALYSIS_LAYER_PATTERNS = [
    re.compile(r'^B0014\d{3}$'),   # 등고선 (주곡선/간곡선/조곡선/계곡선/보조곡선)
    re.compile(r'^F0017\d{3}$'),   # 표고점
    re.compile(r'^C0076\d{3}$'),   # 하천 (폭별)
    re.compile(r'^C0052\d{3}$'),   # 하천 경계/호안
]


def _layer_kept(name: str) -> bool:
    """분석용 모드에서 이 레이어를 남길지 여부"""
    if not name or name in ('0', 'Defpoints'):
        return False
    return any(p.match(name) for p in ANALYSIS_LAYER_PATTERNS)


# =============================================
# 엔티티 복사 — 수치지형도에 등장하는 모든 타입 처리
# =============================================
def _copy_entity(e, target):
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
                pl = target.add_polyline2d(pts, dxfattribs=base)
                if e.is_closed:
                    try: pl.close(True)
                    except Exception: pass
                return 1

        elif t == 'LINE':
            target.add_line(
                start=tuple(e.dxf.start)[:2],
                end=tuple(e.dxf.end)[:2],
                dxfattribs=base
            )
            return 1

        elif t == 'POINT':
            target.add_point(e.dxf.location, dxfattribs=base)
            return 1

        elif t == 'CIRCLE':
            target.add_circle(e.dxf.center, e.dxf.radius, dxfattribs=base)
            return 1

        elif t == 'ARC':
            target.add_arc(
                center=e.dxf.center,
                radius=e.dxf.radius,
                start_angle=e.dxf.start_angle,
                end_angle=e.dxf.end_angle,
                dxfattribs=base
            )
            return 1

        elif t == 'TEXT':
            txt = target.add_text(e.dxf.text, dxfattribs={
                **base,
                'height': e.dxf.get('height', 1),
                'rotation': e.dxf.get('rotation', 0),
            })
            try:
                txt.dxf.insert = e.dxf.insert
            except Exception:
                pass
            return 1

        elif t == 'MTEXT':
            # MTEXT는 R12에 없으므로 TEXT로 변환
            try:
                txt = target.add_text(e.text, dxfattribs={
                    **base,
                    'height': e.dxf.get('char_height', 1),
                    'rotation': e.dxf.get('rotation', 0),
                })
                txt.dxf.insert = e.dxf.insert
                return 1
            except Exception:
                pass

        elif t == 'INSERT':
            target.add_blockref(
                e.dxf.name,
                e.dxf.get('insert', (0, 0, 0)),
                dxfattribs={
                    **base,
                    'xscale': e.dxf.get('xscale', 1),
                    'yscale': e.dxf.get('yscale', 1),
                    'rotation': e.dxf.get('rotation', 0),
                }
            )
            return 1
    except Exception:
        return 0
    return 0


# =============================================
# 변환 메인 함수
# =============================================
def _convert_dxf_to_r12(src_path: str, mode: str = 'original'):
    """
    mode:
      - 'original': 모든 레이어 유지, R12로만 변환
      - 'analysis': 등고선+표고점+하천+수계 레이어만 남기고 R12 변환
    """
    try:
        src = ezdxf.readfile(src_path, encoding='cp949')
    except Exception:
        src = ezdxf.readfile(src_path)

    new = ezdxf.new('R12')
    new_msp = new.modelspace()

    # 레이어 복사 (analysis 모드면 화이트리스트만)
    kept_layers = set()
    for layer in src.layers:
        name = layer.dxf.name
        if name in ('0', 'Defpoints'):
            continue
        if mode == 'analysis' and not _layer_kept(name):
            continue
        try:
            new.layers.new(name, dxfattribs={'color': layer.dxf.get('color', 7)})
            kept_layers.add(name)
        except Exception:
            pass

    # 블록 정의 복사 (모두 시도)
    for bdef in src.blocks:
        if bdef.name.startswith('*'):
            continue
        try:
            nb = new.blocks.new(bdef.name)
            for e in bdef:
                _copy_entity(e, nb)
        except Exception:
            pass

    # 모델스페이스 엔티티 복사
    copied = 0
    skipped = 0
    layer_stats = {}
    for e in src.modelspace():
        layer = e.dxf.get('layer', '0')

        if mode == 'analysis' and layer not in kept_layers:
            skipped += 1
            continue

        n = _copy_entity(e, new_msp)
        if n:
            copied += n
            layer_stats[layer] = layer_stats.get(layer, 0) + n
        else:
            skipped += 1

    # 헤더 복사
    for k in ('$EXTMIN', '$EXTMAX', '$LIMMIN', '$LIMMAX'):
        try:
            new.header[k] = src.header[k]
        except Exception:
            pass

    buf = io.StringIO()
    new.write(buf, fmt='asc')
    out_bytes = buf.getvalue().encode('cp949', errors='replace')

    return out_bytes, {
        'mode': mode,
        'src_version': src.dxfversion,
        'kept_layers': len(kept_layers),
        'copied_entities': copied,
        'skipped_entities': skipped,
    }


# =============================================
# Flask 엔드포인트
# =============================================
@app.route('/api/convert-dxf', methods=['POST'])
def api_convert_dxf():
    """
    DXF R12 변환 + 선택적 레이어 필터링
    
    Form data:
      - file: 업로드된 DXF 파일
      - mode: 'original' (기본) 또는 'analysis'
    """
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
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        out_bytes, stats = _convert_dxf_to_r12(tmp_path, mode=mode)

        base = os.path.splitext(os.path.basename(f.filename))[0]
        suffix = '_R12' if mode == 'original' else '_분석용'
        out_name = f"{base}{suffix}.dxf"

        resp = send_file(
            io.BytesIO(out_bytes),
            as_attachment=True,
            download_name=out_name,
            mimetype='application/octet-stream'
        )
        resp.headers['X-Convert-Mode'] = mode
        resp.headers['X-Src-Version'] = str(stats['src_version'])
        resp.headers['X-Kept-Layers'] = str(stats['kept_layers'])
        resp.headers['X-Copied-Entities'] = str(stats['copied_entities'])
        resp.headers['X-Skipped-Entities'] = str(stats['skipped_entities'])
        resp.headers['Access-Control-Expose-Headers'] = 'X-Convert-Mode, X-Src-Version, X-Kept-Layers, X-Copied-Entities, X-Skipped-Entities'
        return resp

    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'detail': traceback.format_exc().split('\n')[-3:]
        }), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
