"""
화성시 전자민원창구 크롤러
eminwon.hscity.go.kr → 접수번호로 상세 정보 파싱

추출 항목:
  접수번호, 최종담당자, 최종전화번호, 현재상태,
  보완보정 요구 차수, 요구통보일자, 요구완료일자, 요구내용

사용 방법:
  from hwaseong_crawl import crawl_minwon
  result = crawl_minwon("202655300000023378")
  print(result)
"""

import requests
from bs4 import BeautifulSoup
import json
import sys
import time

BASE_URL = "https://eminwon.hscity.go.kr/emwp/gov/mogaha/ntis/web/caf/mwwd/action/CafMwWdOpenAction.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://eminwon.hscity.go.kr/",
}


def _text(tag):
    """태그에서 텍스트를 깨끗하게 추출"""
    if tag is None:
        return ""
    return " ".join(tag.get_text(separator=" ").split())


def crawl_minwon(case_no: str, timeout: int = 15) -> dict:
    """
    접수번호 하나를 받아서 상세 정보를 반환합니다.

    Returns:
        {
          "ok": True/False,
          "error": "에러 메시지 (ok=False일 때)",
          "접수번호": "...",
          "민원사무명": "...",
          "접수일자": "...",
          "처리기한": "...",
          "현재상태": "처리중 / 보완보정 / 완료 ...",
          "최종담당자": "...",
          "최종전화번호": "...",
          "처리부서": "...",
          "보완목록": [
            {
              "차수": "1",
              "요구통보일자": "2026-03-18",
              "요구완료일자": "2026-03-31",
              "요구내용": "...",
              "보완회신내용": "..."
            },
            ...
          ]
        }
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # ── STEP 1: 목록 페이지 GET (세션 쿠키 확보) ──
    list_params = {
        "method": "selectListMwOpn",
        "menu_id": "CAFOPNWebMwOpenL",
        "jndinm": "CafMwWdOpenEJB",
        "methodnm": "selectListMwOpn",
        "context": "NTIS",
        "field": "mw_take_no",
        "keyword": case_no,
        "pageIndex": "1",
        "pageSize": "20",
    }
    try:
        r0 = session.get(BASE_URL, params=list_params, timeout=timeout)
        r0.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "서버 연결 실패 — 화성시 민원 시스템이 현재 점검 중일 수 있습니다."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "요청 시간 초과 (timeout)"}
    except Exception as e:
        return {"ok": False, "error": f"목록 요청 오류: {e}"}

    # 목록 페이지에서 접수번호 존재 확인
    soup0 = BeautifulSoup(r0.text, "html.parser")
    # 실제 데이터 행 확인
    rows = soup0.select("table tbody tr")
    found = any(case_no in str(row) for row in rows)
    if not found:
        return {"ok": False, "error": f"접수번호 {case_no}를 목록에서 찾을 수 없습니다. (비공개 또는 존재하지 않는 번호)"}

    time.sleep(0.5)  # 서버 부하 방지

    # ── STEP 2: 상세 페이지 POST ──
    detail_data = {
        "method": "selectMwOpnDtl",
        "menu_id": "CAFOPNWebMwOpenL",
        "jndinm": "CafMwWdOpenEJB",
        "methodnm": "selectMwOpnDtl",
        "context": "NTIS",
        "mw_take_no": case_no,
    }
    try:
        r1 = session.post(BASE_URL, data=detail_data, timeout=timeout)
        r1.raise_for_status()
        r1.encoding = "utf-8"
    except Exception as e:
        return {"ok": False, "error": f"상세 조회 오류: {e}"}

    soup = BeautifulSoup(r1.text, "html.parser")

    # ── 파싱 시작 ──
    result = {
        "ok": True,
        "접수번호": case_no,
        "민원사무명": "",
        "접수일자": "",
        "처리기한": "",
        "현재상태": "",
        "최종담당자": "",
        "최종전화번호": "",
        "처리부서": "",
        "보완목록": [],
    }

    # ── 접수내역 테이블 파싱 ──
    # <th>접수번호</th> 가 있는 테이블
    all_tables = soup.find_all("table")
    for tbl in all_tables:
        ths = [_text(th) for th in tbl.find_all("th")]
        tds = [_text(td) for td in tbl.find_all("td")]

        # 접수 테이블
        if "접수번호" in ths and "민원사무명" in ths:
            for row in tbl.find_all("tr"):
                cells = row.find_all(["th", "td"])
                for j, cell in enumerate(cells):
                    label = _text(cell)
                    if cell.name == "th" and j + 1 < len(cells):
                        val = _text(cells[j + 1])
                        if label == "민원사무명":
                            result["민원사무명"] = val
                        elif label == "접수일자":
                            result["접수일자"] = val
                        elif label == "처리기한":
                            result["처리기한"] = val

        # 현재상태 테이블 — 처리구분, 처리담당자, 전화번호
        if "처리구분" in ths or "처리담당자" in ths:
            for row in tbl.find_all("tr"):
                cells = row.find_all(["th", "td"])
                for j, cell in enumerate(cells):
                    label = _text(cell)
                    if cell.name == "th" and j + 1 < len(cells):
                        val = _text(cells[j + 1])
                        if label == "처리구분":
                            result["현재상태"] = val
                        elif label == "처리담당자":
                            result["최종담당자"] = val
                        elif label == "처리부서":
                            result["처리부서"] = val
                        elif label == "전화번호":
                            result["최종전화번호"] = val

    # ── 보완보정 요구 섹션 파싱 ──
    # <div> 안에 "보완보정 요구 [N]차" 형태의 span을 찾음
    import re
    sections = soup.find_all("div")
    sup_tables = []

    for div in sections:
        span = div.find("span")
        if span:
            txt = _text(span)
            m = re.search(r"보완보정\s*요구\s*\[?(\d+)\]?\s*차", txt)
            if m:
                차수 = m.group(1)
                # 이 div 바로 다음 table 찾기
                nxt = div.find_next_sibling("table")
                if nxt:
                    sup_tables.append((차수, nxt))

    for 차수, tbl in sup_tables:
        sup = {
            "차수": 차수,
            "요구통보일자": "",
            "요구완료일자": "",
            "요구방법": "",
            "요구사유": "",
            "요구내용": "",
            "보완회신내용": "",
        }
        for row in tbl.find_all("tr"):
            cells = row.find_all(["th", "td"])
            for j, cell in enumerate(cells):
                if cell.name == "th":
                    label = _text(cell)
                    # 같은 행에서 바로 다음 td 찾기
                    next_td = cell.find_next_sibling("td")
                    if next_td:
                        val = _text(next_td)
                        if label == "요구통보일자":
                            sup["요구통보일자"] = val
                        elif label == "요구완료일자":
                            sup["요구완료일자"] = val
                        elif label == "요구방법":
                            sup["요구방법"] = val
                        elif label == "요구사유":
                            sup["요구사유"] = val
                        elif label == "요구내역":
                            sup["요구내용"] = val
                        elif label == "보완회신내용":
                            sup["보완회신내용"] = val
        result["보완목록"].append(sup)

    # 현재상태 fallback — 진행단계 span에서 마지막 활성 상태 추출
    if not result["현재상태"]:
        process_spans = soup.select(".mwpro-list .btn_process")
        if process_spans:
            result["현재상태"] = _text(process_spans[-1])

    # 최종 담당자 fallback — 진행내역 테이블 마지막 행
    if not result["최종담당자"]:
        prog_tbl = soup.find("table", class_="mwpro-table")
        if prog_tbl:
            rows = prog_tbl.find_all("tr")[1:]  # 헤더 제외
            if rows:
                last = rows[-1].find_all("td")
                if len(last) >= 2:
                    result["최종담당자"] = _text(last[1])
                if len(last) >= 5:
                    result["최종전화번호"] = _text(last[4])
                if len(last) >= 3:
                    result["처리부서"] = _text(last[2])

    return result


# ── CLI 실행 (index.html에서 호출용) ──
# python hwaseong_crawl.py <접수번호> → JSON 출력
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "접수번호를 인수로 전달하세요."}, ensure_ascii=False))
        sys.exit(1)

    case_no = sys.argv[1].strip()
    data = crawl_minwon(case_no)
    print(json.dumps(data, ensure_ascii=False, indent=2))
