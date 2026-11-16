"""보고서·기획서 PDF 양식. 내용(view)은 report.build()가 만들고 여기서는 배치만 한다 — 수치·상태를 새로 계산하지 않는다.
구성: 제목 띠 → 문서 정보 → 핵심 카드 → 구역 제목 → 패널·표·흐름 → 주석(내용 가까이) → 부록(실행 기록). 쪽 번호와 작성 방식은 모든 쪽 아래에 찍는다.
상태는 색만으로 구분하지 않고 글자로 적는다."""
import re
from fpdf import FPDF
from fpdf.fonts import FontFace

INK, SUB, DARK, PANEL, SOFT, LINE = (25, 28, 32), (98, 107, 120), (72, 72, 72), (245, 244, 240), (247, 247, 247), (220, 223, 228)
TYPES = {"status": "현황·성과 보고서", "plan": "실험 기획서"}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}:\d{2}(?::\d{2})?))?")


def disp(s):
    """표시 규칙(화면의 disp와 동일): 날짜는 2026.07.21, 제목·항목 구분은 ' — ' 대신 ': '. 저장값은 바꾸지 않는다."""
    return _ISO.sub(lambda m: f"{m[1]}.{m[2]}.{m[3]}" + (f" {m[4]}" if m[4] else ""), str(s)).replace(" — ", ": ")


class Doc(FPDF):
    def __init__(self, font, bold, running, foot):
        super().__init__(format="A4")
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(True, 18)
        self.add_font("kr", "", str(font))
        self.add_font("kr", "B", str(bold))
        self.running, self.foot = disp(running), disp(foot)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("kr", "", 8)
        self.set_text_color(*SUB)
        self.cell(0, 5, self.running, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(5)

    def footer(self):
        self.set_y(-13)
        self.set_font("kr", "", 8)
        self.set_text_color(*SUB)
        self.cell(self.epw * 0.75, 5, self.foot)
        self.cell(self.epw * 0.25, 5, f"{self.page_no()} / {{nb}}", align="R")

    # ── 구성 요소
    def room(self, h):
        if self.will_page_break(h):
            self.add_page()

    def para(self, text, size=9.5, color=INK, bold=False, h=5.2, gap=1.5):
        self.set_font("kr", "B" if bold else "", size)
        self.set_text_color(*color)
        self.multi_cell(0, h, disp(text), align="L", new_x="LMARGIN", new_y="NEXT")
        self.ln(gap)

    def note(self, text):
        self.para("주) " + text, 8.2, SUB, h=4.4, gap=2.5)

    def banner(self, crumb, title, sub):
        x, y, w = self.l_margin, self.get_y(), self.epw
        self.set_font("kr", "", 10)
        sub_h = self.multi_cell(w - 20, 5.5, disp(sub), dry_run=True, output="HEIGHT") if sub else 0
        hgt = 34 + sub_h
        self.set_fill_color(*DARK)
        self.rect(x, y, w, hgt, style="F")
        self.set_fill_color(88, 88, 88)
        self.rect(x + w * 0.78, y, w * 0.22, hgt, style="F")
        self.set_text_color(255, 255, 255)
        self.set_xy(x + 10, y + 8)
        self.set_font("kr", "", 9)
        self.cell(w - 20, 5, disp(crumb), new_x="LMARGIN", new_y="NEXT")
        self.set_x(x + 10)
        self.set_font("kr", "B", 20)
        self.cell(w - 20, 12, disp(title), new_x="LMARGIN", new_y="NEXT")
        if sub:
            self.set_x(x + 10)
            self.set_font("kr", "", 10)
            self.multi_cell(w - 20, 5.5, disp(sub), align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_y(y + hgt + 6)

    def meta(self, pairs):
        """문서 정보: 이름·값을 두 쌍씩 한 줄에. 아래위로 가는 선을 그어 본문과 구분한다."""
        half, kw, lh = self.epw / 2, 21, 4.8
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        for i in range(0, len(pairs), 2):
            y, hmax = self.get_y(), lh
            for n, (k, v) in enumerate(pairs[i:i + 2]):
                x = self.l_margin + n * half
                self.set_xy(x, y)
                self.set_font("kr", "", 8.5)
                self.set_text_color(*SUB)
                self.cell(kw, lh, disp(k))
                self.set_font("kr", "", 8.8)
                self.set_text_color(*INK)
                self.set_xy(x + kw, y)
                self.multi_cell(half - kw - 3, lh, disp(v), align="L")
                hmax = max(hmax, self.get_y() - y)
            self.set_y(y + hmax + 1.2)
        self.ln(1)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def h2(self, title, lead=None):
        self.room(48)  # 제목만 쪽 끝에 남지 않게 한다
        self.ln(3)
        self.set_font("kr", "B", 14)
        self.set_text_color(*INK)
        self.cell(0, 8, disp(title), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*INK)
        self.set_line_width(0.6)
        self.line(self.l_margin, self.get_y() + 0.5, self.l_margin + 12, self.get_y() + 0.5)
        self.set_line_width(0.2)
        self.ln(4)
        if lead:
            self.para(lead, 9, SUB, gap=2.5)

    def h3(self, title):
        self.room(16)
        self.para(title, 10.5, INK, bold=True, h=6, gap=1)

    def panel(self, title, items, tag=None):
        """어두운 머리띠 + 옅은 본문. 항목마다 줄을 나눈다."""
        w = self.epw
        self.room(22)
        self.set_fill_color(*DARK)
        self.set_text_color(255, 255, 255)
        self.set_font("kr", "B", 10)
        self.cell(w, 8, "  " + disp(title) + (f"   [{tag}]" if tag else ""), fill=True, new_x="LMARGIN", new_y="NEXT")
        items = [x for x in (items or []) if x not in (None, "")] or ["해당 없음"]
        for i, it in enumerate(items):
            txt = disp(it)
            self.set_font("kr", "", 9.5)
            h = self.multi_cell(w - 12, 5.2, txt, dry_run=True, output="HEIGHT") + (3 if i == 0 else 1.4) + (2 if i == len(items) - 1 else 0)
            self.room(h)
            y = self.get_y()
            self.set_fill_color(*PANEL)
            self.rect(self.l_margin, y, w, h, style="F")
            self.set_text_color(*INK)
            self.set_font("kr", "", 9.5)
            self.set_xy(self.l_margin + 4, y + (2.4 if i == 0 else 0.8))
            self.cell(4, 5.2, "•")
            self.multi_cell(w - 12, 5.2, txt, align="L", new_x="LMARGIN", new_y="NEXT")
            self.set_y(y + h)
        self.ln(4)

    def cards(self, items):
        """핵심 수치 카드: (이름, 값, 설명)."""
        n, gap, hgt = len(items), 4, 30
        w = (self.epw - gap * (n - 1)) / n
        self.room(hgt + 5)
        y = self.get_y()
        for i, (lab, val, sub) in enumerate(items):
            x = self.l_margin + i * (w + gap)
            self.set_fill_color(*SOFT)
            self.rect(x, y, w, hgt, style="F")
            self.set_xy(x + 4, y + 3.5)
            self.set_font("kr", "", 8.5)
            self.set_text_color(*SUB)
            self.cell(w - 8, 4.5, disp(lab))
            size, val = 14, disp(val)
            self.set_font("kr", "B", size)
            while self.get_string_width(val) > w - 8 and size > 8:
                size -= 0.5
                self.set_font("kr", "B", size)
            self.set_xy(x + 4, y + 9)
            self.set_text_color(*INK)
            self.cell(w - 8, 8, val)
            self.set_xy(x + 4, y + 18.5)
            self.set_font("kr", "", 8)
            self.set_text_color(*SUB)
            self.multi_cell(w - 8, 3.9, disp(sub), align="L")
        self.set_y(y + hgt + 5)

    def grid(self, head, rows, widths, aligns=None, size=8.8):
        """표. 쪽을 넘기면 열 제목을 다시 찍는다."""
        if not rows:
            self.para("해당 없음", 9, SUB, gap=3)
            return
        self.room(24)
        self.set_font("kr", "", size)
        self.set_text_color(*INK)
        self.set_draw_color(*LINE)
        tot = sum(widths)
        with self.table(col_widths=tuple(self.epw * x / tot for x in widths), text_align=tuple(aligns or ["LEFT"] * len(head)), line_height=4.8, padding=1.6,
                        borders_layout="HORIZONTAL_LINES", headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=DARK),
                        cell_fill_color=(250, 250, 248), cell_fill_mode="ROWS", repeat_headings=1, v_align="TOP") as tb:
            r = tb.row()
            for c in head:
                r.cell(disp(c))
            for row in rows:
                r = tb.row()
                for c in row:
                    r.cell(disp("—" if c in (None, "") else c))
        self.ln(3)

    def flow(self, steps):
        """단계 흐름: (단계, 상태, 현재 여부). 현재 단계는 어두운 칸과 '◀ 현재' 글자로 함께 표시한다."""
        n, gap, hgt = len(steps), 4, 19
        w = (self.epw - gap * (n - 1)) / n
        self.room(hgt + 5)
        y = self.get_y()
        for i, (lab, state, cur) in enumerate(steps):
            x = self.l_margin + i * (w + gap)
            self.set_fill_color(*(DARK if cur else SOFT))
            self.rect(x, y, w, hgt, style="F")
            self.set_text_color(*((255, 255, 255) if cur else INK))
            self.set_font("kr", "B", 8.3)
            self.set_xy(x, y + 2.5)
            self.cell(w, 4.6, disp(lab), align="C")
            self.set_font("kr", "", 7.6)
            self.set_xy(x + 1, y + 7.8)
            self.multi_cell(w - 2, 3.6, disp(state) + ("\n◀ 현재" if cur else ""), align="C")
            if i < n - 1:
                self.set_text_color(*SUB)
                self.set_font("kr", "", 9)
                self.set_xy(x + w, y + 6)
                self.cell(gap, 6, "›", align="C")
        self.set_y(y + hgt + 5)


# ── 문서 유형별 배치
def _head(doc, body, v):
    doc.banner(f"{body['tenant']}", TYPES.get(v.get("type"), "총괄 보고"), v.get("goal") or body.get("title"))
    doc.meta([("업무 기준일", body["as_of"]), ("작성 일시", body["created_at"]), ("보고 상태", body["run_status"]), ("작성 방식", v.get("authoring", "규칙 기반 / LLM 미연결")),
              ("요청자", v.get("requester", "—")), ("요청 작업", v.get("workflow_label", "—")), ("요청 해석", v.get("llm_note", "—")), ("문서 번호", v.get("doc_no", "—"))])


def _status(doc, body, v):
    _head(doc, body, v)
    doc.h2("핵심 현황")
    doc.cards(v["kpis"])
    if v.get("ai_summary"):
        doc.panel("요약", [v["ai_summary"]], tag="연결된 모델의 문장 초안 · 수치는 계산 결과")
    doc.panel("가장 중요한 위험·확인 필요 사항", v["risks"])
    doc.panel("의사결정자가 확인·결정할 사항", v["decide"], tag="시스템은 자동 확정하지 않음")
    doc.panel("다음 행동", v["next"], tag="규칙 기반 제안")

    doc.h2("주요 성과", "국가별 기준선은 과거 주문 표본의 관측값이다. 완료된 A/B 실험 결과가 아니다.")
    p = v.get("perf")
    if p:
        doc.grid(p["head"], p["rows"], [16] + [17] * (len(p["head"]) - 1), ["LEFT"] + ["RIGHT"] * (len(p["head"]) - 1))
        for n in p["notes"]:
            doc.note(n)
    else:
        doc.para("국가별 기준선: 이번 실행에서는 계산하지 않았다.", 9.5, SUB, gap=3)
    a = v.get("analysis")
    if a:
        doc.h3(f"실험 #{a['exp_id']} 성과 분석 — 판정: {a['verdict']}")
        doc.grid(a["head"], a["rows"], [8, 13, 13, 15, 15, 13, 30], ["LEFT"] + ["RIGHT"] * 5 + ["LEFT"])
        for n in a["notes"]:
            doc.note(n)
        if a["holds"]:
            doc.panel("판단 보류 사유·주의", a["holds"])

    doc.h2("재무·승인 현황", "두 전망은 정보 성격이 다르다. 시스템은 어느 쪽도 정답으로 확정하지 않는다.")
    f = v.get("fin")
    if f:
        doc.grid(["항목", "재무팀 공식 전망", "원본 스케줄 재전망"], f["rows"], [22, 39, 39])
        for n in f["notes"]:
            doc.note(n)
        if f.get("scope_facts"):
            doc.panel("전망 범위·산정 기준 차이: 확인된 사실", f["scope_facts"], tag="원본·계산 코드 대조")
            doc.panel("전망 범위·산정 기준 차이: 후보 원인", f["scope_candidates"], tag="미확인 · 재무 담당 확인 필요")
        doc.panel("확인되지 않은 가정·절차", f["unconfirmed"], tag="담당자 확인 필요")
        if f.get("gate"):
            doc.panel(f["gate"]["title"], f["gate"]["reasons"])
    else:
        doc.para("재무 검토: 이번 실행에서 수행하지 않았다.", 9.5, SUB, gap=3)

    doc.h2("실험 및 후속 작업")
    if v["exps"]:
        doc.grid(["실험", "국가 / 채널", "대상 상품", "진행 상태", "승인권자", "다음 작업(담당)"], v["exps"], [8, 22, 14, 16, 14, 26])
    else:
        doc.para("등록된 실험 없음.", 9.5, SUB, gap=3)
    doc.h3(f"열린 작업 {len(v['todos'])}건")
    doc.grid(["종류", "담당 역할", "내용"], v["todos"], [14, 20, 66])
    doc.h3(f"담당자 확인 요청 {len(v['requests'])}건")
    doc.grid(["항목", "담당", "상태", "요청 사유"], v["requests"], [8, 15, 30, 47])
    if v["requests"]:
        doc.note("회신은 승인이 아니다. '가상 회신'은 시험용 입력이며 실제 담당자 회신이 아니다.")
    _evidence(doc, v)


def _plan(doc, body, v):
    p = v["plan"]
    _head(doc, body, v)
    doc.h2("기획 개요")
    doc.cards(p["kpis"])
    doc.panel("기획 배경과 해결할 문제", p["background"])
    doc.panel("이 기획서의 성격", ["총괄 에이전트가 만든 설계 초안이다. 담당자가 항목을 확정해 입력해야 상신할 수 있다.",
                             "'확인 필요'로 적힌 항목은 원본에 없어 시스템이 정하지 않았다.", "승인되지 않았다. 아래 '승인 절차와 현재 상태'가 실제 상태다."])

    doc.h2("실험 설계", "가설, A/B안, 실험군·비교군, 기간, 지표와 기준.")
    doc.grid(["항목", "내용", "정보 구분"], p["design"], [22, 54, 24])
    doc.panel("총괄의 제안", p["suggestions"], tag="규칙 기반 제안 · 담당자 확정 전에는 기준으로 쓰이지 않음")
    doc.h3("핵심 지표와 측정 방식")
    doc.panel("측정·판정 규칙", p["measure"])

    doc.h2("예산·일정")
    doc.grid(["항목", "내용", "정보 구분"], p["schedule"], [22, 54, 24])
    doc.note("일정·예산·담당자는 원본에 없다. 미입력 항목은 시스템이 정하지 않는다.")

    doc.h2("관련 부서 제약", "재고·공급, 운영조건, 재무(현금) 제약이 상신 가능 여부와 승인 경로를 바꾼다.")
    for title, items, tag in p["constraints"]:
        doc.panel(title, items, tag=tag)

    doc.h2("승인 절차와 현재 상태")
    doc.flow(p["flow"])
    doc.panel(p["gate_title"], p["gate_reasons"])
    doc.panel("실행 후 담당 작업", p["after"], tag="참가자 설계 A-016")
    doc.h3(f"이 실험의 열린 작업 {len(p['todos'])}건")
    doc.grid(["종류", "담당 역할", "내용"], p["todos"], [14, 20, 66])

    doc.h2("위험, 가정 및 정보 공백")
    doc.panel("입력·확인이 필요한 항목", p["missing"], tag="확인 필요")
    _evidence(doc, v)


def _evidence(doc, v):
    doc.h2("데이터 한계 및 근거")
    doc.panel("한계·충돌·가정", v["limits"])
    if v["sources"]:
        doc.h3("근거 문서(원문 위치)")
        doc.grid(["문서", "행", "발췌"], v["sources"], [28, 9, 63], size=8.4)
    doc.panel("사용한 원본 자료", v["source_sets"] or ["이번 실행에서 읽은 원본 없음"])
    if v.get("files"):
        doc.h3(f"요청에 첨부된 파일 {len(v['files'])}건(원본 데이터와 구분)")
        doc.grid(["파일", "크기", "처리 상태", "사용 여부", "내용"], v["files"], [22, 9, 20, 10, 39], size=8.4)
        doc.note("첨부 파일은 읽을 수 있는 형식만 구조(행·열 수)를 확인했다. 파일의 값은 지표·재무 계산에 쓰지 않았다. '사용 안 함'은 읽지 않았다는 뜻이다.")


def _legacy(doc, body, v):
    """view가 없는 이전 보고서: 11개 항목을 같은 양식의 패널로 보여 준다."""
    _head(doc, body, v)
    doc.para(body.get("notice", ""), 9, SUB, gap=3)
    for s in body["sections"]:
        doc.panel(s["title"], s["lines"])


def render(path, body, tasks, font, bold):
    """PDF를 쓰고 쪽 수를 돌려준다. tasks = 이 실행의 단계 기록(쓰는 시점의 실제 상태)."""
    v = body.get("view") or {}
    doc = Doc(font, bold, f"{body['tenant']} · {TYPES.get(v.get('type'), '총괄 보고')}", f"업무 기준일 {body['as_of']} · 작성 방식: {v.get('authoring', '규칙 기반 / LLM 미연결')}")
    doc.add_page()
    {"status": _status, "plan": _plan}.get(v.get("type"), _legacy)(doc, body, v)
    doc.add_page()
    doc.h2("부록: 실행 기록", "이번 실행에서 실제로 수행한 단계와 정책 검사 결과. '사용자 확인 필요'는 단계 실행은 끝났고 담당자 확인이 남았다는 뜻이다.")
    doc.grid(["순서", "역할", "상태", "정책 검사", "결과 요약"], tasks, [7, 16, 18, 12, 47])
    if v.get("type"):
        doc.h3("항목별 기록(화면의 11개 항목과 같은 내용)")
        for s in body["sections"]:
            doc.panel(s["title"], s["lines"])
    doc.output(str(path))
    return doc.page_no()
