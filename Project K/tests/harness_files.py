"""파일 분석 시험 하네스. 시험용 엑셀·PDF를 코드로 만들고(합성 시험 자료), 정답표와 추출 결과를 대조한다.
실제 모델(LLM)은 쓰지 않는다. 나중에 다른 추출기(예: 모델 기반)를 붙이면 같은 정답표로 `score()`에 넣어 비교할 수 있다.

쓰임: tests/run_tests.py의 H1~H7. 단독 실행하면 현재 추출기의 채점표를 출력한다.
    python tests/harness_files.py"""
import io, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import extract  # noqa: E402

FONT = "C:/Windows/Fonts/malgun.ttf"
HEAD = ["arm", "period_start", "period_end", "impressions", "clicks", "orders", "gmv_krw", "refund_krw", "ad_cost_krw", "product_cost_krw", "channel_fee_krw", "shipping_krw", "return_logistics_krw"]
ROW_A = ["A", "2026-09-07", "2026-09-20", 100000, 3000, 150, 9000000, 800000, 2500000, 3300000, 1170000, 450000, 60000]
ROW_B = ["B", "2026-09-07", "2026-09-20", 100000, 3600, 210, 12600000, 900000, 2500000, 4620000, 1638000, 630000, 70000]


def _ref(c, r):
    return extract._letters(c) + str(r)


def make_xlsx(sheets, merged=None, formula_without_value=None):
    """sheets = {이름: [[값...], ...]}. 글자는 공유 문자열로, 숫자는 숫자 칸으로 쓴다. None은 빈 칸."""
    strings, idx = [], {}

    def si(s):
        if s not in idx:
            idx[s] = len(strings)
            strings.append(s)
        return idx[s]

    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    files = {}
    for n, (name, rows) in enumerate(sheets.items(), 1):
        body = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row, 1):
                if v is None:
                    continue
                ref = _ref(c, r)
                if formula_without_value == ref:
                    cells.append(f'<c r="{ref}"><f>SUM(A1:A2)</f></c>')
                elif isinstance(v, str):
                    cells.append(f'<c r="{ref}" t="s"><v>{si(v)}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        mg = f'<mergeCells count="{len(merged)}">{"".join(f"<mergeCell ref=\"{m}\"/>" for m in merged)}</mergeCells>' if merged and n == 1 else ""
        files[f"xl/worksheets/sheet{n}.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><worksheet {ns}><sheetData>{"".join(body)}</sheetData>{mg}</worksheet>'
    files["xl/sharedStrings.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><sst {ns}>{"".join(f"<si><t>{esc(s)}</t></si>" for s in strings)}</sst>'
    files["xl/workbook.xml"] = (f'<?xml version="1.0" encoding="UTF-8"?><workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                               + "".join(f'<sheet name="{esc(name)}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(sheets, 1)) + "</sheets></workbook>")
    files["xl/_rels/workbook.xml.rels"] = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                          + "".join(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{n}.xml"/>'
                                                    for n in range(1, len(sheets) + 1)) + "</Relationships>")
    files["[Content_Types].xml"] = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def make_pdf(pages):
    """pages = [[줄, ...], ...]. 글자 층이 있는 PDF. 줄이 리스트면 같은 높이에 칸을 나눠 찍는다(표 흉내)."""
    from fpdf import FPDF
    import logging
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    pdf = FPDF()
    pdf.add_font("kr", "", FONT)
    for lines in pages:
        pdf.add_page()
        pdf.set_font("kr", "", 11)
        for ln in lines:
            if isinstance(ln, (list, tuple)):
                for cell in ln:
                    pdf.cell(45, 8, str(cell))
                pdf.ln(8)
            else:
                pdf.cell(0, 8, ln, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def make_scanned_pdf():
    """글자 층 없이 그림만 있는 PDF(스캔 흉내)."""
    from fpdf import FPDF
    from PIL import Image
    img = io.BytesIO()
    Image.new("RGB", (200, 80), (230, 230, 230)).save(img, "PNG")
    pdf = FPDF()
    pdf.add_page()
    pdf.image(io.BytesIO(img.getvalue()), x=20, y=20, w=100)
    return bytes(pdf.output())


# 정답표: (이름, 파일 내용, 추출 함수, 확인할 값 {출처: 기대값})
def fixtures():
    return [
        ("성과.xlsx", make_xlsx({"성과": [HEAD, ROW_A, ROW_B], "메모": [["항목", "내용"], ["가설", "배송비 포함가가 실질 ROAS를 높인다"]]}), "xlsx",
         {"성과!A2": "A", "성과!F2": 150, "성과!G3": 12600000, "성과!M3": 70000, "메모!B2": "배송비 포함가가 실질 ROAS를 높인다"}),
        ("결측.xlsx", make_xlsx({"성과": [HEAD, ROW_A, ["B", "2026-09-07", "2026-09-20", 100000, 3600, 210, 12600000, 900000, 2500000, None, 1638000, 630000, 70000]]}), "xlsx",
         {"성과!F3": 210, "성과!J3": None}),
        ("보고.pdf", make_pdf([["해외 실험 성과 요약", ["안", "주문", "GMV"], ["A", "150", "9,000,000"], ["B", "210", "12,600,000"]], ["둘째 쪽: 반품 환불 900,000원"]]), "pdf",
         {"1쪽 1줄": "해외 실험 성과 요약", "1쪽 3줄": "A  150  9,000,000", "1쪽 4줄": "B  210  12,600,000", "2쪽 1줄": "둘째 쪽: 반품 환불 900,000원"}),
    ]


def lookup(ex, src):
    """출처 문자열로 추출 결과에서 값을 찾는다. 없으면 None."""
    if ex["kind"] == "pdf":
        p, ln = (int(x.rstrip("쪽줄")) for x in src.split())
        lines = ex["pages"][p - 1]["lines"] if p <= len(ex["pages"]) else []
        return lines[ln - 1] if ln <= len(lines) else None
    sheet, ref = src.split("!")
    sh = next((s for s in ex["sheets"] if s["name"] == sheet), None)
    row = sh and next((r for r in sh["rows"] if r["row"] == int("".join(ch for ch in ref if ch.isdigit()))), None)
    return row["cells"].get("".join(ch for ch in ref if ch.isalpha())) if row else None


def score(extractor=None):
    """추출기를 정답표로 채점한다. extractor(kind, data) -> 추출 결과. 기본은 현재 규칙 기반 추출기."""
    extractor = extractor or (lambda kind, data: getattr(extract, kind)(data))
    rows = []
    for name, data, kind, expect in fixtures():
        try:
            ex = extractor(kind, data)
            got = {k: lookup(ex, k) for k in expect}
            err = None
        except Exception as e:  # 추출 실패도 채점 결과다
            got, err = {}, f"{type(e).__name__}: {e}"
        rows.append({"file": name, "total": len(expect), "correct": sum(got.get(k) == v for k, v in expect.items()), "error": err,
                     "wrong": {k: (got.get(k), v) for k, v in expect.items() if got.get(k) != v}})
    return rows


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for r in score():
        print(f"{r['file']}: {r['correct']}/{r['total']}" + (f" · 오류 {r['error']}" if r["error"] else "") + (f" · 불일치 {r['wrong']}" if r["wrong"] else ""))
