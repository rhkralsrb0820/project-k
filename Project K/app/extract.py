"""첨부 파일에서 읽을 수 있는 글자·표·수치를 꺼낸다. 표준 라이브러리만 쓴다(외부 전송·실행·매크로 실행 없음).
모든 값에는 출처(시트!셀, PDF 쪽·줄, 텍스트 줄 번호)가 붙는다. 읽지 못한 것은 읽지 못했다고 적고 값을 추측하지 않는다.

한계(그대로 화면에 표시한다):
- 엑셀: .xlsx만. 수식은 파일에 저장된 계산값만 읽는다(다시 계산하지 않음). 병합 셀이 있으면 표 구조가 불명확하다고 표시한다. .xls(구형)·암호 걸린 파일은 읽지 못한다.
- PDF: 글자 층이 있는 PDF만. 스캔(이미지) PDF는 글자를 읽지 못한다(문자 인식 없음). 표는 복원하지 않고 같은 높이의 글자를 한 줄로 잇기만 한다.
  암호화된 PDF, 글자 대응표(ToUnicode)가 없는 글꼴은 읽지 못한다."""
import csv, datetime, io, re, zipfile, zlib
import xml.etree.ElementTree as ET

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MAX_UNZIPPED, MAX_ROWS, MAX_PAGES = 60 * 1024 * 1024, 2000, 200
OBS_NUM = ["impressions", "clicks", "orders", "gmv_krw", "refund_krw", "ad_cost_krw", "product_cost_krw", "channel_fee_krw", "shipping_krw", "return_logistics_krw"]


class Unreadable(Exception):
    """읽을 수 없는 파일. 메시지가 사용자에게 보이는 사유다."""


def _col(ref):
    return re.match(r"[A-Z]+", ref)[0]


def _colnum(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ───────── 엑셀(.xlsx)
def is_xlsx(data):
    try:
        return "xl/workbook.xml" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return False


def xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unreadable("엑셀 파일이 손상되어 열 수 없음")
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise Unreadable("압축을 풀었을 때 크기가 너무 커서 읽지 않음")
    if "xl/workbook.xml" not in z.namelist():
        raise Unreadable("엑셀 통합 문서가 아님")

    def xml(name):
        raw = z.read(name)
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise Unreadable("안전하지 않은 XML 선언이 있어 읽지 않음")
        return ET.fromstring(raw)

    try:
        shared = ["".join(x.text or "" for x in si.iter(M + "t")) for si in xml("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in z.namelist() else []
        datefmt = set()
        if "xl/styles.xml" in z.namelist():
            st = xml("xl/styles.xml")
            custom = {int(f.get("numFmtId")): f.get("formatCode", "") for f in st.iter(M + "numFmt")}
            xfs = st.find(M + "cellXfs")
            for i, xf in enumerate(xfs if xfs is not None else []):
                fid = int(xf.get("numFmtId", 0))
                if 14 <= fid <= 22 or 45 <= fid <= 47 or re.search(r"[ymd]", re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fid, "")), re.I):
                    datefmt.add(i)
        rels = {r.get("Id"): r.get("Target") for r in xml("xl/_rels/workbook.xml.rels")}
        sheets = []
        for s in xml("xl/workbook.xml").find(M + "sheets"):
            target = rels[s.get(RID)].lstrip("/")
            root = xml(target if target.startswith("xl/") else "xl/" + target)
            rows, issues = {}, []
            for c in root.iter(M + "c"):
                ref, typ, v, f = c.get("r"), c.get("t"), c.find(M + "v"), c.find(M + "f")
                if v is None or v.text is None:
                    val = "".join(x.text or "" for x in c.iter(M + "t")) if typ == "inlineStr" else None
                    if f is not None and val is None:
                        issues.append(f"{ref}: 수식의 계산값이 파일에 저장되지 않아 읽지 못함")
                    if val in (None, ""):
                        continue
                elif typ == "s":
                    val = shared[int(v.text)]
                elif typ == "b":
                    val = v.text == "1"
                elif typ == "e":
                    issues.append(f"{ref}: 셀 오류값 {v.text}")
                    continue
                elif typ in ("str", "inlineStr"):
                    val = v.text
                else:
                    x = float(v.text)
                    if int(c.get("s", 0)) in datefmt:
                        val = (datetime.datetime(1899, 12, 30) + datetime.timedelta(days=x)).isoformat()[:10 if x == int(x) else 19]
                    else:
                        val = int(x) if x == int(x) else x
                rows.setdefault(int(re.search(r"\d+", ref)[0]), {})[_col(ref)] = val
            merged = [m.get("ref") for m in root.iter(M + "mergeCell")]
            if merged:
                issues.append(f"병합 셀 {len(merged)}곳({', '.join(merged[:5])}{' 등' if len(merged) > 5 else ''}) — 표 구조가 불명확할 수 있음")
            if len(rows) > MAX_ROWS:
                issues.append(f"행이 {len(rows):,}개라 처음 {MAX_ROWS:,}행만 읽음")
            keep = sorted(rows)[:MAX_ROWS]
            cols = sorted({k for r in keep for k in rows[r]}, key=_colnum)
            sheets.append({"name": s.get("name"), "hidden": s.get("state") in ("hidden", "veryHidden"), "columns": cols, "issues": issues,
                           "rows": [{"row": r, "cells": rows[r]} for r in keep], "table": _table(s.get("name"), [(r, rows[r]) for r in keep], cols)})
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(f"엑셀 구조를 읽는 중 오류({type(e).__name__})")
    return {"kind": "xlsx", "sheets": sheets, "limits": "수식은 저장된 계산값만 읽음. 매크로·외부 연결은 읽지 않음"}


def _table(sheet, rows, cols):
    """머리글 행이 뚜렷한 표만 표로 인정한다. 반환 {header_row, columns{열: 이름}, issues} 또는 사유."""
    if not rows:
        return {"ok": False, "reason": "빈 시트"}
    r0, first = rows[0]
    names = {c: str(first[c]).strip() for c in cols if isinstance(first.get(c), str) and str(first[c]).strip()}
    if len(names) < 2 or len(names) != len(first):
        return {"ok": False, "reason": f"{sheet}!{r0}행을 머리글로 볼 수 없음(글자가 아닌 칸이 있거나 칸이 1개)"}
    if len(set(names.values())) != len(names):
        return {"ok": False, "reason": f"{sheet}!{r0}행에 같은 머리글이 두 번 있음 — 표 구조 불명확"}
    return {"ok": True, "header_row": r0, "columns": names}


# ───────── 구분자 텍스트(CSV·TSV)
def delimited(text):
    lines = text.splitlines()
    head = lines[0] if lines else ""
    delim = "|" if "|" in head else "\t" if "\t" in head else ","
    rows = []
    for n, r in enumerate(csv.reader(lines, delimiter=delim), 1):
        if r:
            rows.append((n, {_letters(i + 1): (v.strip() if v.strip() not in ("", "NONE") else None) for i, v in enumerate(r)}))
    rows = [(n, {k: v for k, v in cells.items() if v is not None}) for n, cells in rows][:MAX_ROWS]
    cols = sorted({k for _, c in rows for k in c}, key=_colnum)
    return {"kind": "csv", "sheets": [{"name": "본문", "hidden": False, "columns": cols, "issues": [], "rows": [{"row": n, "cells": c} for n, c in rows],
                                      "table": _table("본문", rows, cols)}], "limits": "행 번호는 파일의 줄 번호. 빈 칸과 NONE은 결측"}


# ───────── 표 → 성과 관측값 후보(사용자 검토 뒤에만 쓴다)
def observations(sheet):
    """머리글에 arm과 orders가 있는 표만 대상. 숫자가 아닌 값은 고치지 않고 오류로 돌려준다. 빈 칸은 결측(None)."""
    tb = sheet["table"]
    if not tb.get("ok"):
        return {"ok": False, "reason": tb["reason"], "rows": [], "errors": []}
    by = {name.lower(): col for col, name in tb["columns"].items()}
    if "arm" not in by or "orders" not in by:
        return {"ok": False, "reason": "성과 입력 형식이 아님(머리글에 arm과 orders가 있어야 함)", "rows": [], "errors": []}
    out, errors = [], []
    for r in sheet["rows"]:
        if r["row"] <= tb["header_row"]:
            continue
        ref = lambda k: f"{sheet['name']}!{by[k]}{r['row']}"
        cell = lambda k: r["cells"].get(by[k]) if k in by else None
        row = {"arm": str(cell("arm") or "").strip().upper(), "period_start": cell("period_start"), "period_end": cell("period_end"), "_row": r["row"], "_refs": {}, "_missing": []}
        if row["arm"] not in ("A", "B"):
            errors.append(f"{ref('arm')}: arm 값이 A 또는 B가 아님({cell('arm')!r})")
        for k in OBS_NUM:
            v = cell(k)
            if v is None:
                row[k] = None
                row["_missing"].append(k)
            elif isinstance(v, bool) or not isinstance(v, (int, float)) and not re.fullmatch(r"-?\d+(\.\d+)?", str(v).strip()):
                errors.append(f"{ref(k)}: 숫자가 아닌 값 {v!r} — 추측해서 고치지 않음")
                row[k] = None
            else:
                row[k] = float(v)
                row["_refs"][k] = ref(k)
                if row[k] < 0:
                    errors.append(f"{ref(k)}: 음수 {v}")
        for k in ("period_start", "period_end"):
            if row[k] is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row[k])):
                errors.append(f"{ref(k)}: 날짜 형식(YYYY-MM-DD)이 아님({row[k]!r})")
        out.append(row)
    if not out:
        return {"ok": False, "reason": "자료 행이 없음", "rows": [], "errors": []}
    return {"ok": not errors, "reason": "오류가 있는 칸을 고친 파일을 다시 첨부해야 가져올 수 있음" if errors else None, "rows": out, "errors": errors,
            "unknown_columns": [n for n in tb["columns"].values() if n.lower() not in ["arm", "period_start", "period_end"] + OBS_NUM]}


# ───────── PDF(글자 층만)
def _stream(body):
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return None
    raw = body[m.end():body.rfind(b"endstream")]
    head = body[:m.start()]
    if b"/Filter" in head:
        if not re.search(rb"/Filter\s*(\[\s*)?/FlateDecode\s*\]?", head) or re.search(rb"/Filter\s*\[[^\]]*/\w+[^\]]*/\w+", head):
            return None  # Flate 하나만 읽는다(이미지 등 다른 압축은 건너뜀)
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    return raw


def _sub(body, key, objs, depth=0):
    """사전에서 key의 값(다른 객체 참조 또는 안에 든 사전)을 바이트로 돌려준다."""
    m = re.search(rb"/" + key + rb"\s*(\d+)\s+\d+\s+R", body)
    i = body.find(b"/" + key)
    if i < 0:
        return None
    rest = body[i + len(key) + 1:].lstrip()
    if rest.startswith(b"<<"):
        d, j = 0, 0
        while j < len(rest):
            if rest[j:j + 2] == b"<<":
                d += 1
                j += 2
            elif rest[j:j + 2] == b">>":
                d -= 1
                j += 2
                if d == 0:
                    return rest[:j]
            else:
                j += 1
        return rest
    if m and m.start() == i and depth < 5:
        return objs.get(int(m[1]))
    return None


def _cmap(data):
    mp, size = {}, 1
    for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\n?(?=[^<]*endcodespacerange)", data):
        size = max(size, len(a) // 2)
    u = lambda h: bytes.fromhex(h.decode()).decode("utf-16-be", "replace") if len(h) % 4 == 0 else chr(int(h, 16))
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", blk):
            size = max(size, len(a) // 2)
            mp[int(a, 16)] = u(b) if b else ""
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for a, b, arr, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:\[(.*?)\]|<([0-9A-Fa-f]+)>)", blk, re.S):
            size = max(size, len(a) // 2)
            lo, hi = int(a, 16), int(b, 16)
            if dst:
                base = u(dst)
                for k in range(min(hi - lo + 1, 65536)):
                    mp[lo + k] = base[:-1] + chr(ord(base[-1]) + k) if base else ""
            else:
                for k, h in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", arr)):
                    mp[lo + k] = u(h)
    return mp, size


def _tokens(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i:i + 1]
        if c in b" \t\r\n\x00\x0c":
            i += 1
        elif c == b"%":
            j = s.find(b"\n", i)
            i = n if j < 0 else j
        elif c == b"(":
            d, j, out = 1, i + 1, bytearray()
            while j < n and d:
                ch = s[j:j + 1]
                if ch == b"\\":
                    e = s[j + 1:j + 2]
                    if e in b"nrtbf":
                        out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}[e]
                        j += 2
                    elif e.isdigit():
                        o = re.match(rb"[0-7]{1,3}", s[j + 1:j + 4])[0]
                        out.append(int(o, 8) & 255)
                        j += 1 + len(o)
                    elif e in b"\r\n":
                        j += 3 if s[j + 1:j + 3] == b"\r\n" else 2
                    else:
                        out += e
                        j += 2
                    continue
                if ch == b"(":
                    d += 1
                elif ch == b")":
                    d -= 1
                    if not d:
                        break
                out += ch
                j += 1
            yield ("s", bytes(out))
            i = j + 1
        elif c == b"<" and s[i:i + 2] != b"<<":
            j = s.find(b">", i)
            h = re.sub(rb"\s", b"", s[i + 1:j])
            yield ("s", bytes.fromhex((h + b"0" if len(h) % 2 else h).decode("ascii", "ignore")) if re.fullmatch(rb"[0-9A-Fa-f]*", h) else b"")
            i = j + 1
        elif c in b"[]":
            yield (c.decode(), None)
            i += 1
        elif c == b"/":
            m = re.match(rb"/[^\s/\[\]<>()%]*", s[i:])
            yield ("n", m[0][1:].decode("latin-1"))
            i += len(m[0])
        elif s[i:i + 2] in (b"<<", b">>"):
            i += 2
        else:
            m = re.match(rb"[^\s/\[\]<>()%]+", s[i:])
            if not m:
                i += 1
                continue
            tok = m[0]
            i += len(tok)
            try:
                yield ("d", float(tok))
            except ValueError:
                yield ("o", tok.decode("latin-1"))


def _page_text(content, fonts):
    frags, stack, font, x, y, lx, ly, bad, arr = [], [], None, 0.0, 0.0, 0.0, 0.0, 0, None

    def dec(b):
        nonlocal bad
        f = fonts.get(font)
        if f is None:
            return b.decode("latin-1")
        mp, size = f
        if not mp:
            bad += len(b) // size
            return ""
        out = []
        for k in range(0, len(b) - size + 1, size):
            ch = mp.get(int.from_bytes(b[k:k + size], "big"))
            if ch is None:
                bad += 1
            else:
                out.append(ch)
        return "".join(out)

    for kind, v in _tokens(content):
        if kind == "[":
            arr = []
        elif kind == "]":
            stack.append(arr)
            arr = None
        elif arr is not None and kind in ("s", "d"):
            arr.append((kind, v))
        elif kind != "o":
            stack.append(v)
        else:
            a = stack
            try:
                if v == "BT":
                    x = y = lx = ly = 0.0
                elif v == "Tf":
                    font = a[-2]
                elif v in ("Td", "TD"):
                    lx, ly = lx + a[-2], ly + a[-1]
                    x, y = lx, ly
                elif v == "Tm":
                    lx, ly = a[-2], a[-1]
                    x, y = lx, ly
                elif v == "T*":
                    ly -= 12
                    x, y = lx, ly
                elif v in ("Tj", "'", '"'):
                    if v != "Tj":
                        ly -= 12
                        x, y = lx, ly
                    frags.append((round(y, 1), x, dec(a[-1])))
                    x += 0.01
                elif v == "TJ":
                    frags.append((round(y, 1), x, "".join(dec(p) if k == "s" else (" " if p < -250 else "") for k, p in a[-1])))
                    x += 0.01
            except (IndexError, TypeError):
                pass
            stack = []
    lines = {}
    for yy, xx, txt in frags:
        if txt.strip():
            lines.setdefault(yy, []).append((xx, txt))
    return ["  ".join(tx.strip() for _, tx in sorted(v)) for _, v in sorted(lines.items(), key=lambda kv: -kv[0])], bad


def pdf(data):
    if not data.startswith(b"%PDF"):
        raise Unreadable("PDF 형식이 아님")
    if re.search(rb"/Encrypt\s", data):
        raise Unreadable("암호화된 PDF라 읽을 수 없음")
    objs = {}
    for m in re.finditer(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", data, re.S):
        objs[int(m[1])] = m[2]
    for body in list(objs.values()):
        if re.search(rb"/Type\s*/ObjStm", body[:400]):
            dec = _stream(body)
            n, first = re.search(rb"/N\s+(\d+)", body), re.search(rb"/First\s+(\d+)", body)
            if dec is None or not n or not first:
                continue
            nums = [int(v) for v in dec[:int(first[1])].split()]
            for k in range(int(n[1])):
                off = int(first[1]) + nums[2 * k + 1]
                end = int(first[1]) + nums[2 * k + 3] if 2 * k + 3 < len(nums) else len(dec)
                objs.setdefault(nums[2 * k], dec[off:end])
    if not objs:
        raise Unreadable("PDF 구조를 읽을 수 없음(객체를 찾지 못함)")
    is_page = lambda b: re.search(rb"/Type\s*/Page(?![a-zA-Z])", b)
    pages = []

    def walk(num, res, seen):
        b = objs.get(num)
        if b is None or num in seen or len(pages) >= MAX_PAGES:
            return
        seen.add(num)
        r = _sub(b, b"Resources", objs) or res
        if is_page(b):
            pages.append((b, r))
        else:
            for k in re.findall(rb"(\d+)\s+\d+\s+R", (re.search(rb"/Kids\s*\[(.*?)\]", b, re.S) or [b"", b""])[1]):
                walk(int(k), r, seen)

    cat = next((b for b in objs.values() if re.search(rb"/Type\s*/Catalog", b)), None)
    root = cat and re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", cat)
    if root:
        walk(int(root[1]), None, set())
    if not pages:
        pages = [(b, _sub(b, b"Resources", objs)) for _, b in sorted(objs.items()) if is_page(b)][:MAX_PAGES]
    if not pages:
        raise Unreadable("PDF에서 쪽을 찾지 못함")
    cache, out = {}, []
    for no, (b, res) in enumerate(pages, 1):
        fonts = {}
        fd = _sub(res, b"Font", objs) if res else None
        for name, num in re.findall(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R", fd or b""):
            num = int(num)
            if num not in cache:
                fb = objs.get(num, b"")
                tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", fb)
                st = _stream(objs.get(int(tu[1]), b"")) if tu else None
                cache[num] = _cmap(st) if st else (({}, 2) if re.search(rb"/Subtype\s*/Type0", fb) else None)
            fonts[name.decode("latin-1")] = cache[num]
        cm = re.search(rb"/Contents\s*(\[(.*?)\]|(\d+)\s+\d+\s+R)", b, re.S)
        refs = [int(k) for k in re.findall(rb"(\d+)\s+\d+\s+R", cm[2])] if cm and cm[2] is not None else [int(cm[3])] if cm else []
        content = b"\n".join(filter(None, (_stream(objs.get(k, b"")) for k in refs)))
        lines, bad = _page_text(content, fonts)
        xo = _sub(res, b"XObject", objs) if res else None
        img = bool(xo and any(re.search(rb"/Subtype\s*/Image", objs.get(int(k), b"")) for k in re.findall(rb"(\d+)\s+\d+\s+R", xo)))
        note = None
        if not lines:
            note = "글자 층이 없음 — 스캔(이미지) 쪽으로 보임. 글자를 읽지 못함" if img else "읽을 글자가 없음" + ("(글꼴의 글자 대응표가 없어 해독하지 못함)" if bad else "")
        elif bad:
            note = f"해독하지 못한 글자 {bad}개(글꼴의 글자 대응표에 없음)"
        out.append({"page": no, "lines": lines, "note": note, "has_image": img})
    return {"kind": "pdf", "pages": out, "limits": "글자 층만 읽음. 표는 복원하지 않고 같은 높이의 글자를 한 줄로 이었음. 문자 인식(OCR) 없음"}
