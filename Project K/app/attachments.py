"""사용자 첨부 파일. 원본 데이터(01_참가자_배포용)와 분리해 상태 DB의 attachments 표에 내용까지 저장한다.
- 파일은 번호(id)로만 다룬다. 파일명은 표시용이며 경로로 쓰지 않는다 → 같은 이름이어도 덮어쓰지 않는다.
- 어떤 형식이든 첨부할 수 있지만, 내용을 읽는 것은 실제로 읽어 본 UTF-8 텍스트(CSV·TSV·TXT·MD·JSON)뿐이다.
- 첨부만으로 아무것도 실행하지 않는다. 압축을 풀지 않는다. 외부로 보내지 않는다.
- 삭제 기능은 없다(data.delete는 고정 금지 행동). 제출 전에 목록에서 빼면 업로드되지 않는다.
역할 기반 제어는 시연용이다(사용자 인증 미구현)."""
import csv, hashlib, json, re
from . import extract
from .core import now, dumps, ALLOW

MAX_BYTES, MAX_FILES = 10 * 1024 * 1024, 10
READABLE = "엑셀(.xlsx), 글자 층이 있는 PDF, UTF-8 텍스트(CSV·TSV·TXT·MD·JSON)"
OK, STORED, FAIL = "내용 분석 가능", "첨부 완료 / 현재 내용 분석 미지원", "처리 실패"
EXEC = {"exe", "dll", "bat", "cmd", "ps1", "sh", "py", "js", "vbs", "msi", "jar", "com", "scr"}
MAGIC = [(b"%PDF", "PDF"), (b"PK\x03\x04", "압축·오피스 문서(zip 계열: xlsx·docx·pptx·zip)"), (b"MZ", "실행 파일"), (b"\x89PNG", "PNG 이미지"), (b"GIF8", "GIF 이미지"),
         (b"\xff\xd8", "JPEG 이미지"), (b"Rar!", "압축(rar)"), (b"7z\xbc\xaf", "압축(7z)"), (b"\x1f\x8b", "압축(gzip)"), (b"\xd0\xcf\x11\xe0", "구형 오피스 문서(xls·doc)")]
COLS = "id,name,size,sha256,status,detail,owner_type,owner_id,uploaded_by,uploaded_at"


def clean_name(name):
    """파일명에 들어 있는 경로는 믿지 않는다. 마지막 조각만, 제어문자 없이, 200자까지."""
    n = re.sub(r"[\x00-\x1f]", "", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()[:200]
    return n or "이름 없는 파일"


def inspect(name, data):
    """내용을 실제로 읽어 보고 판정한다. 확장자는 참고만 한다. 반환 (상태, 상세)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in EXEC:
        return STORED, {"reason": "실행·스크립트 형식은 첨부만 하고 읽거나 실행하지 않는다"}
    if data.startswith(b"PK\x03\x04") and (ext in ("xlsx", "xlsm") or extract.is_xlsx(data)):
        try:
            ex = extract.xlsx(data)
        except extract.Unreadable as e:
            return FAIL, {"reason": str(e)}
        issues = [f"{sh['name']}: {i}" for sh in ex["sheets"] for i in sh["issues"]] + [f"{sh['name']}: {sh['table']['reason']}" for sh in ex["sheets"] if not sh["table"]["ok"]]
        return OK, {"format": "엑셀(xlsx)", "sheets": [{"name": sh["name"], "rows": len(sh["rows"]), "columns": len(sh["columns"]), "table_ok": sh["table"]["ok"]} for sh in ex["sheets"]],
                    "summary": "엑셀 " + ", ".join(f"[{sh['name']}] {len(sh['rows'])}행 × {len(sh['columns'])}열" for sh in ex["sheets"]), "note": " / ".join(issues[:4]) if issues else None}
    if data.startswith(b"%PDF"):
        try:
            ex = extract.pdf(data)
        except extract.Unreadable as e:
            return (STORED if "암호화" in str(e) else FAIL), {"reason": str(e)}
        text = [p for p in ex["pages"] if p["lines"]]
        if not text:
            return STORED, {"reason": "글자 층이 없는 PDF(스캔으로 보임): 글자를 읽지 못한다(문자 인식 기능 없음)", "pages": len(ex["pages"])}
        blank = [p["page"] for p in ex["pages"] if not p["lines"]]
        return OK, {"format": "PDF", "pages": len(ex["pages"]), "text_pages": len(text), "unread_pages": blank,
                    "summary": f"PDF {len(ex['pages'])}쪽 중 글자를 읽은 쪽 {len(text)}쪽, {sum(len(p['lines']) for p in text):,}줄", "note": f"글자를 읽지 못한 쪽: {blank}" if blank else None}
    for sig, label in MAGIC:
        if data.startswith(sig):
            return STORED, {"reason": f"{label}: 이 형식을 읽는 기능이 없다" + ("(압축은 풀지 않는다)" if "압축" in label else ""), "detected": label}
    if b"\0" in data[:8192]:
        return STORED, {"reason": "텍스트가 아닌 형식이라 읽지 않는다"}
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return STORED, {"reason": "UTF-8 텍스트가 아니라 읽지 않는다(다른 문자 인코딩이거나 이진 형식)"}
    lines = text.splitlines()
    d = {"lines": len(lines), "format": "텍스트"}
    head = lines[0] if lines else ""
    if ext == "json":
        try:
            json.loads(text)
            d["format"] = "JSON"
            d["summary"] = f"JSON, {len(lines):,}줄"
        except ValueError as e:
            return FAIL, {"reason": f"JSON 형식 오류: {e}"}
    elif ext in ("csv", "tsv") or any(x in head for x in "|\t"):
        delim = "|" if "|" in head else "\t" if "\t" in head else ","
        rows = [r for r in csv.reader(lines, delimiter=delim) if r]
        if rows and len(rows[0]) > 1:
            cols = [c.strip() for c in rows[0]]
            ragged = sum(len(r) != len(cols) for r in rows[1:])
            d.update(format=f"표(구분자 {'탭' if delim == chr(9) else delim})", columns=cols[:40], column_count=len(cols), rows=len(rows) - 1, ragged_rows=ragged,
                     observation_format="arm" in cols and "orders" in cols)
            d["summary"] = f"{d['format']}, {d['rows']:,}행 × {d['column_count']}열"
            if ragged:
                d["note"] = f"열 수가 머리글과 다른 행 {ragged}개"
        elif ext in ("csv", "tsv"):
            return FAIL, {"reason": "표 형식으로 읽히지 않는다(열이 1개뿐 — 구분자 확인 필요)"}
    d.setdefault("summary", f"{d['format']}, {d['lines']:,}줄")
    return OK, d


def _row(r):
    r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
    r["link"] = f"총괄 요청 #{r['owner_id']}" if r["owner_type"] == "run" else f"실험 #{r['owner_id']}" if r["owner_type"] == "experiment" else "연결 전"
    return r


def upload(t, user, name, data):
    t.require(user, "attachment.upload")
    name = clean_name(name)
    if not data:
        raise ValueError(f"'{name}': 빈 파일은 첨부할 수 없음")
    if len(data) > MAX_BYTES:
        raise ValueError(f"'{name}': 파일 크기 {len(data) / 1048576:.1f}MB가 제한 {MAX_BYTES // 1048576}MB를 넘음")
    status, detail = inspect(name, data)
    aid = t.q("INSERT INTO attachments(name,size,sha256,content,status,detail,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
              (name, len(data), hashlib.sha256(data).hexdigest(), data, status, dumps(detail), user, now()))
    return _row(t.q(f"SELECT {COLS} FROM attachments WHERE id=?", (aid,))[0])


def check_link(t, user, ids):
    """연결할 수 있는지 확인만 한다. 자기가 올린, 아직 연결되지 않은 파일만 연결할 수 있다."""
    ids = [int(i) for i in (ids or [])]
    if len(ids) > MAX_FILES:
        raise ValueError(f"한 번에 {MAX_FILES}개까지 첨부할 수 있음")
    for i in ids:
        r = t.q("SELECT uploaded_by, owner_type FROM attachments WHERE id=?", (i,))
        if not r:
            raise KeyError(f"첨부 #{i} 없음")
        if r[0]["uploaded_by"] != user or r[0]["owner_type"]:
            raise PermissionError(f"첨부 #{i}: 본인이 올린 연결 전 파일만 연결할 수 있음")
    return ids


def link(t, user, ids, owner_type, owner_id):
    """제출된 요청·실험에 연결한다."""
    ids = check_link(t, user, ids)
    for i in ids:
        t.q("UPDATE attachments SET owner_type=?, owner_id=? WHERE id=?", (owner_type, owner_id, i))
    return ids


def _can_view(t, user, r):
    if t.check(user, "attachment.view", f"attachment:{r['id']}")["decision"] != ALLOW:
        return False
    role = t.policy()["roles"].get(t.role_of(user)) or {}
    ok = (r["uploaded_by"] == user or "policy.edit" in role.get("actions", []) or r["owner_type"] == "experiment"
          or (r["owner_type"] == "run" and "run.start" in role.get("actions", [])))
    if not ok:  # 요청 첨부는 총괄에게 요청할 수 있는 역할만, 연결 전 파일은 올린 사람과 관리자만
        t.q("INSERT INTO audit(ts,actor,via,action,resource,decision,reason) VALUES(?,?,?,?,?,?,?)",
            (now(), user, None, "attachment.view", f"attachment:{r['id']}", "금지", f"'{r['link'] if 'link' in r else r['owner_type']}' 첨부를 볼 수 있는 역할이 아님"))
    return ok


def list_for(t, user, owner_type, owner_id):
    rows = [_row(r) for r in t.q(f"SELECT {COLS} FROM attachments WHERE owner_type=? AND owner_id=? ORDER BY id", (owner_type, owner_id))]
    if rows and not _can_view(t, user, rows[0]):
        raise PermissionError(f"'{user}'는 이 첨부 목록을 볼 수 없음")
    return rows


def download(t, user, aid):
    r = t.q(f"SELECT {COLS}, content FROM attachments WHERE id=?", (aid,))
    if not r:
        raise KeyError(f"첨부 #{aid} 없음")
    r = _row(r[0])
    if not _can_view(t, user, r):
        raise PermissionError(f"'{user}'는 첨부 #{aid}를 내려받을 수 없음")
    return r


def use_in_run(t, run_id):
    """총괄이 요청에 첨부된 파일을 확인한다. 읽을 수 있는 파일만 구조(행·열)를 확인하고, 무엇을 썼는지 돌려준다.
    첨부 파일의 값은 지표·재무 계산에 넣지 않는다."""
    out = []
    for r in (_row(x) for x in t.q(f"SELECT {COLS} FROM attachments WHERE owner_type='run' AND owner_id=? ORDER BY id", (run_id,))):
        f = {"id": r["id"], "name": r["name"], "size": r["size"], "status": r["status"], "used": False}
        if r["status"] != OK:
            f["note"] = "읽지 않음 — " + r["detail"].get("reason", r["status"])
        else:
            d = t.check("orchestrator", "attachment.read", f"attachment:{r['id']}")
            if d["decision"] != ALLOW:
                f["note"] = f"읽지 않음 — 정책 검사 {d['decision']}: {d['reason']}"
            else:
                x = r["detail"]
                f["used"] = True
                f["note"] = ("구조 확인에 사용: " + x["summary"] + (" · 성과 입력 형식과 일치(실험에 첨부하면 검토 후 가져올 수 있음, 자동으로 넣지 않음)" if x.get("observation_format") else "")
                             + (f" · {x['note']}" if x.get("note") else "") + ". 값은 계산에 쓰지 않음")
        out.append(f)
    return out


def extracted(t, user, aid):
    """추출 결과 보기(출처 포함). 보기 권한을 검사한다. 실험에 연결된 표는 성과 입력 후보와 현재 입력값과의 차이도 함께 준다."""
    r = download(t, user, aid)
    data = r.pop("content")
    if r["status"] != OK:
        raise ValueError(f"첨부 #{aid}: 내용을 읽을 수 없는 파일 — {r['detail'].get('reason', r['status'])}")
    if data.startswith(b"%PDF"):
        ex = extract.pdf(data)
    elif data.startswith(b"PK"):
        ex = extract.xlsx(data)
    elif "rows" in r["detail"]:
        ex = extract.delimited(data.decode("utf-8-sig"))
    else:
        lines = data.decode("utf-8-sig").splitlines()
        ex = {"kind": "text", "lines": lines[:500], "limits": f"전체 {len(lines):,}줄 중 처음 {min(len(lines), 500)}줄"}
    for sh in ex.get("sheets", []):
        sh["observations"] = extract.observations(sh)
        if r["owner_type"] == "experiment" and sh["observations"]["rows"]:
            sh["observations"]["compare"] = compare(t, r["owner_id"], sh["observations"]["rows"])
            sh["observations"]["imported"] = bool(t.q("SELECT 1 FROM observations WHERE exp_id=? AND source LIKE ?", (r["owner_id"], f"첨부 #{aid} %[{sh['name']}]%")))
    return {"attachment": r, "extraction": ex}


def compare(t, exp_id, rows):
    """근거 대조: 실험에 지금 입력돼 있는 성과(안별 합계)와 파일의 값(안별 합계)이 다른 칸을 찾는다. 입력이 없으면 빈 목록."""
    have = {}
    for o in t.q("SELECT arm, data FROM observations WHERE exp_id=?", (exp_id,)):
        d = json.loads(o["data"])
        for k in extract.OBS_NUM:
            have.setdefault(o["arm"], {}).setdefault(k, []).append(d.get(k))
    diffs = []
    for arm in sorted(have):
        mine = [r for r in rows if r["arm"] == arm]
        for k in extract.OBS_NUM:
            a, b = have[arm][k], [r[k] for r in mine]
            sa, sb = (None if any(v is None for v in a) else sum(a)), (None if not b or any(v is None for v in b) else sum(b))
            if sa != sb:
                diffs.append({"arm": arm, "field": k, "entered": sa, "file": sb})
    return diffs


def import_observations(t, user, exp_id, aid, sheet, confirmed):
    """사용자가 추출 값을 검토·확인한 뒤에만 성과 입력으로 가져온다. 이 실험에 첨부된 파일만, 오류 없는 표만, 한 번만.
    승인·재무 판정에는 손대지 않는다 — 기존 성과 입력 경로(권한·상태 검사 포함)를 그대로 탄다."""
    from . import experiments as X
    if confirmed is not True:
        raise ValueError("추출 값을 원본 파일과 대조했다는 사용자 확인이 있어야 가져올 수 있음")
    meta = t.q("SELECT name, owner_type, owner_id FROM attachments WHERE id=?", (aid,))
    if not meta or (meta[0]["owner_type"], meta[0]["owner_id"]) != ("experiment", exp_id):
        raise PermissionError(f"첨부 #{aid}는 실험 #{exp_id}에 첨부된 파일이 아님")
    sh = next((x for x in extracted(t, user, aid)["extraction"].get("sheets", []) if x["name"] == sheet), None)
    if sh is None:
        raise ValueError(f"'{sheet}' 표를 찾을 수 없음(PDF·일반 텍스트는 성과 입력으로 가져올 수 없음)")
    ob = sh["observations"]
    if not ob["ok"]:
        raise ValueError(f"가져올 수 없음: {ob['reason']}" + ("; " + "; ".join(ob["errors"][:5]) if ob["errors"] else ""))
    if ob.get("imported"):
        raise ValueError(f"첨부 #{aid} [{sheet}]는 이미 가져왔음(중복 입력 방지)")
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ob["rows"]]
    return X.add_observations(t, user, exp_id, rows, f"첨부 #{aid} {meta[0]['name']} [{sheet}] · {user} 검토 후 가져옴")
