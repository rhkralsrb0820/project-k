"""Project K 로컬 서버. 실행: python -m uvicorn app.server:app --app-dir "Project K" --port 8765"""
import json, os
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from . import agents, attachments, board, core, experiments as X, finance, llm, metrics, ml, rag, clarifications, supply

T = core.Tenant(os.environ.get("PROJECTK_CONFIG", str(core.ROOT / "config" / "masil_w06.json")), os.environ.get("PROJECTK_DB"))
app = FastAPI(title="Project K")
STATIC = core.ROOT / "app" / "static"

BUILD_STATUS = [
    ("데이터 연결·국가별 기준선 검증", "실제 작동"), ("공식 전망 vs 재전망 비교·하회 구간 특정", "실제 작동"),
    ("A/B 실험 등록·점검·게이트·상태 변경", "실제 작동"), ("성과 입력(직접·CSV)·지표 계산·판단 보류", "실제 작동"),
    ("대상 상품 공급 여력 확인(재고 커버 ↔ 안전재고 하한)·대안 상품 제시·승인 후 후속 작업 생성", "실제 작동"),
    ("총괄 오케스트레이션·정책 검사·실행 기록", "실제 작동(규칙 기반, LLM 미연동)"), ("문서 검색(BM25)·원문 위치", "실제 작동"),
    ("총괄 보고서(현황·성과)·실험 기획서 PDF", "실제 작동"), ("업무함 보드: 관리자 확인 처리(보드에서 내림)·완료함 기록·복구", "실제 작동"),
    ("파일 첨부(총괄 요청·실험 등록)·요청/실험 연결·권한 검사", "실제 작동(역할 제어는 시연용)"),
    ("엑셀(.xlsx)·PDF(글자 층)·텍스트 추출과 출처 표시, 검토 후 성과 입력으로 가져오기", "실제 작동(규칙 기반 추출 — AI 아님. 스캔 PDF·구형 xls·암호 파일은 읽지 못함)"), ("LLM 연결(요청 해석·문장 초안)", "미구현(연결 지점만 준비 — 연결된 모델 없음)"), ("승인·재무 확인·운영조건 회신·SCM 공급 회신·그룹 보고", "모의 처리(가상 입력)"),
    ("실험 집행(게시·광고)", "모의 처리(고객사 직접 수행 표시만)"), ("사용자 인증", "미구현(역할 선택은 시연용)"),
    ("엑셀(xlsx) 직접 가져오기", "미구현(CSV로 저장해 가져오기)"), ("LoRA 학습·동일 평가셋 비교·CPU/CUDA 선택", "합성 자료 CPU 실행만 확인(모의 처리) / 실제 자료 학습 보류 / CUDA 미검증"),
    ("고객 의견·설문 분석, 다국어 메시지 초안", "미구현(데이터 없음)"), ("다른 고객사 적용", "설정 구조만 — 미검증"),
    ("저장소", "SQLite(기본) 또는 PostgreSQL(DATABASE_URL 지정 시 — 쓰기 흐름은 미검증)"),
]


@app.exception_handler(PermissionError)
async def _perm(_: Request, e: PermissionError):
    return JSONResponse({"error": str(e), "type": "차단"}, 403)


@app.exception_handler(ValueError)
async def _val(_: Request, e: ValueError):
    return JSONResponse({"error": str(e), "type": "입력 오류"}, 400)


@app.exception_handler(KeyError)
async def _key(_: Request, e: KeyError):
    return JSONResponse({"error": str(e.args[0]), "type": "없음"}, 404)


def _sources(t):
    """설정에 등록된 자료 목록. 여기 없는 경로는 열 수 없다."""
    return [{"path": d["path"], "name": d["path"].rsplit("/", 1)[-1], "class": d["class"], "ext": d.get("root") == "ext", "scale": d.get("scale", "문서")}
            for d in list(t.cfg["datasets"].values()) + t.cfg["documents"]]


def _memo(key, fn):
    """원본은 실행 중 바뀌지 않으므로 대시보드용 계산은 프로세스당 한 번만 한다(감사 로그 소음 방지)."""
    if key not in T._cache:
        T._cache[key] = fn()
    return T._cache[key]


def dashboard_data(t=T):
    link = _memo("dash_link", lambda: metrics.linkage(t)) if t is T else metrics.linkage(t)
    base = _memo("dash_base", lambda: metrics.baselines(t)) if t is T else metrics.baselines(t)
    scen_key = "dash_fin:" + json.dumps(t.get(finance.SCENARIO_KEY), sort_keys=True, ensure_ascii=False)
    fin = _memo(scen_key, lambda: finance.compare(t)) if t is T else finance.compare(t)
    exps = X.list_all(t)
    todos = t.q("SELECT * FROM todos WHERE status='열림' ORDER BY id")
    rep = t.q("SELECT * FROM reports ORDER BY id DESC LIMIT 1")
    issues = [{"level": "검증 실패", "text": f"연결 검사 {c['id']} {c['name']}: 불일치 {c['unmatched']:,}/{c['total']:,}"} for c in link["checks"] if c["result"] != "성공"]
    issues += [{"level": "검증 실패", "text": f"예비 수치 불일치: {p['country']} {p['metric']} 전달 {p['received']} ↔ 계산 {p['computed']}"} for p in base["preliminary"] if p["result"] != "성공"]
    rc = fin["received_claim_check"]
    if rc and rc["result"] != "성공":
        issues.append({"level": "검증 실패", "text": f"{rc['received']} ↔ 원본 재계산 {rc['computed']}. {rc['note']}"})
    issues += [{"level": "사용자 확인 필요", "text": u} for u in link["unverifiable"] + fin["official"]["unknown"] + fin["policy_findings"]["unconfirmed"]]
    issues += [{"level": "사용자 확인 필요", "text": f"{g['channel_id']} 결제조건: 마스터 {g['master_terms_days']}일 ↔ AR 만기 청구 후 {'·'.join(map(str, g['observed_due_minus_invoice_days']))}일"} for g in fin["term_gap"]]
    issues += [{"level": "데이터 없음", "text": x} for x in ["실험군·비교군·성공·중단 기준: 원본에 없음(실험 등록 시 담당자 입력)", "국가별 배송 약속·통관·현지 운영비·반품 조건: 원본에 없음(해외 운영 확인 요청으로 관리)", "고객 의견·설문 데이터: 원본에 없음"]]

    nxt = [{"who": x["assignee"], "text": f"{x['kind']}: {x['title']}", "exp_id": x["exp_id"]} for x in todos]
    if fin["conflict"]:
        nxt.append({"who": "fin_staff", "text": "공식 전망과 재전망 충돌 — 공식 전망의 입력값·가정(발주대금, 바뀐 결제조건 반영 여부) 확인"})
    plans = fin["budget_plans"]["rows"]
    for dl in sorted({p["contract_deadline"] for p in plans}):
        grp = [p for p in plans if p["contract_deadline"] == dl]
        nxt.append({"who": "masil_ceo", "text": f"캠페인 확대 기안 {len(grp)}건의 확정기한 {dl} — 기한 전 승인 여부 결정 필요(미결정은 묵시적 승인과 같음). "
                                                 f"이 중 집행 예정일이 하회 구간에 드는 건 {sum(p['in_window'] for p in grp)}건"})
    if not exps:
        nxt.append({"who": "mkt_staff", "text": "등록된 실험 없음 — 실험을 등록하거나 총괄에게 설계를 요청"})
    return {
        "tenant": {**{k: t.cfg[k] for k in ("name", "as_of", "countries", "config_version", "tenant_id", "project_id")},
                   "db": "PostgreSQL" if t.pg else t.db_path.name, "db_kind": t.db_kind},
        "users": t.cfg["users"], "baselines": base, "linkage": link, "finance": fin, "experiments": exps, "todos": todos,
        "issues": issues, "next_actions": nxt, "agents": agents.agent_board(t), "information_requests": clarifications.list_requests(t),
        "recent_tasks": t.q("SELECT t.id, t.run_id, t.role, t.title, t.status, t.ended_at, t.output FROM agent_tasks t WHERE status='완료' ORDER BY id DESC LIMIT 8"),
        "runs": t.q("SELECT id, workflow, goal, user, status, started_at, ended_at, report_id FROM runs ORDER BY id DESC LIMIT 6"),
        "latest_report": ({**rep[0], "body": json.loads(rep[0]["body"])} if rep else None),
        "llm": llm.status(t), "archive": board.listing(t), "env": ml.env_status(), "lora": ml.lora_readiness(t), "build_status": BUILD_STATUS,
        "meta": {"metrics": X.METRICS, "obs_fields": X.OBS_FIELDS, "required": X.REQUIRED, "roles": agents.ROLES, "sources": _sources(t), "attach": {"max_mb": attachments.MAX_BYTES // 1048576, "max_files": attachments.MAX_FILES, "readable": attachments.READABLE}, "skus": list(supply.table(t).values()), "workflows": {k: v[0] for k, v in agents.WORKFLOWS.items()}},
    }


@app.get("/healthz")
def healthz():
    """배포 환경의 상태 확인용. DB를 부르지 않고 바로 답한다."""
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/dashboard")
def dashboard():
    return dashboard_data()


@app.get("/api/finance/daily")
def finance_daily():
    ov, label, _ = finance.active_overlay(T)
    return {"label": label, "daily": [x for x in finance.reforecast(T, overlay=ov)["daily"] if x["items"]]}


@app.post("/api/finance/scenario")
def finance_scenario(b: dict = Body(...)):
    return {"regated": X.set_finance_scenario(T, b["user"], b.get("overlay"), b.get("label"), b.get("reason"))}


@app.post("/api/experiments")
def exp_create(b: dict = Body(...)):
    attachments.check_link(T, b["user"], b.get("attachment_ids"))  # 연결할 수 없는 첨부가 있으면 실험을 만들지 않는다
    e = X.create(T, b["user"], b["data"])
    if b.get("attachment_ids"):  # 첨부는 연결만 한다. 필수 항목·재무 검토·승인에는 아무 영향이 없다
        attachments.link(T, b["user"], b["attachment_ids"], "experiment", e["id"])
        T.q("INSERT INTO history(exp_id,ts,actor,event,detail) VALUES(?,?,?,?,?)", (e["id"], core.now(), b["user"], "파일 첨부", f"{len(b['attachment_ids'])}건 연결(내용은 설계 항목에 반영되지 않음)"))
    return e


@app.post("/api/attachments")
async def attachment_upload(request: Request, user: str, name: str):
    """파일 올리기(본문 = 파일 내용 그대로). 저장만 하고 실행·압축 해제·외부 전송은 하지 않는다."""
    if int(request.headers.get("content-length") or 0) > attachments.MAX_BYTES:
        raise ValueError(f"'{attachments.clean_name(name)}': 파일 크기가 제한 {attachments.MAX_BYTES // 1048576}MB를 넘음")
    return attachments.upload(T, user, name, await request.body())


@app.get("/api/attachments")
def attachment_list(user: str, owner_type: str, owner_id: int):
    return attachments.list_for(T, user, owner_type, owner_id)


@app.get("/api/attachments/{aid}/extract")
def attachment_extract(aid: int, user: str):
    return attachments.extracted(T, user, aid)


@app.get("/api/attachments/{aid}/download")
def attachment_download(aid: int, user: str):
    from urllib.parse import quote
    r = attachments.download(T, user, aid)  # 브라우저가 열어 실행하지 않도록 항상 내려받기로만 준다
    return Response(r["content"], media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(r['name'])}", "X-Content-Type-Options": "nosniff"})


@app.get("/api/experiments/{eid}")
def exp_get(eid: int):
    return X.get(T, eid)


@app.post("/api/experiments/{eid}/{action}")
def exp_action(eid: int, action: str, b: dict = Body(...)):
    u = b["user"]
    if action == "update":
        return X.update(T, u, eid, b["data"])
    if action == "submit":
        return X.submit(T, u, eid)
    if action == "ops_confirm":
        return X.ops_confirm(T, u, eid, b.get("note", ""))
    if action == "supply_confirm":
        return X.supply_confirm(T, u, eid, bool(b["available"]), b.get("note", ""))
    if action == "finance_confirm":
        return X.finance_confirm(T, u, eid, b.get("note", ""))
    if action == "decide":
        return X.decide(T, u, eid, bool(b["approve"]), b.get("reason", ""))
    if action == "start":
        return X.start(T, u, eid)
    if action == "observations_from_attachment":
        return attachments.import_observations(T, u, eid, int(b["attachment_id"]), b.get("sheet"), b.get("confirmed"))
    if action == "observations":
        return X.import_csv(T, u, eid, b["csv"]) if b.get("csv") else X.add_observations(T, u, eid, b["rows"])
    if action == "conclude":
        return X.conclude(T, u, eid, b.get("judgment", ""))
    if action == "next":
        return X.next_from(T, u, eid, b.get("changes"))
    raise KeyError(f"지원하지 않는 동작: {action}")


@app.post("/api/board/archive")
def board_archive(b: dict = Body(...)):
    return board.archive(T, b["user"], b.get("key"), b.get("category"), b.get("title"), b.get("meta"), b.get("note"), b.get("exp_id"))


@app.post("/api/board/restore")
def board_restore(b: dict = Body(...)):
    return board.restore(T, b["user"], int(b["id"]))


@app.post("/api/runs")
def run_start(b: dict = Body(...)):
    return agents.run(T, b["user"], b["workflow"], b.get("payload") or {})


@app.get("/api/runs/{rid}")
def run_get(rid: int):
    return agents.get_run(T, rid)


@app.post("/api/clarifications/{cid}")
def clarification_reply(cid: str, b: dict = Body(...)):
    return clarifications.reply(T, b["user"], cid, b.get("text"), b.get("source"))


@app.get("/api/rag")
def rag_search(q: str, user: str):
    return rag.search(T, user, q)


def _country_rows(path, country, lines):
    """국가별 기준선(metrics.baselines)과 같은 귀속 기준으로 그 국가에 해당하는 행만 고른다.
    반환 (원본 행 번호 목록, 기준 설명). 국가를 가를 키가 없는 자료는 None."""
    name = next((k for k, d in T.cfg["datasets"].items() if d["path"] == path and d.get("root") != "ext"), None)
    orders = T.rows("orchestrator", "orders")
    if name in ("orders", "campaigns", "channels"):
        key, ok, why = "country", {country}, "country 컬럼"
    elif name in ("returns", "coupons"):
        key, ok, why = "order_id", {o["order_id"] for o in orders if o["country"] == country}, "원 주문(order_id)의 국가"
    elif name == "ad_spend":
        key, ok, why = "campaign_id", {c["campaign_id"] for c in T.rows("orchestrator", "campaigns") if c["country"] == country}, "캠페인(campaign_id)의 국가"
    elif name == "repurchase":
        cc = {o["customer_ref"]: o["country"] for o in orders}  # 고객 국가 = 주문 국가(A-005·A-006)
        key, ok, why = "customer_ref", {k for k, v in cc.items() if v == country}, "고객(customer_ref)의 주문 국가"
    else:
        return None
    i = lines[0].split("|").index(key)
    return [n for n, l in enumerate(lines[1:], 2) if l.split("|")[i] in ok], why


@app.get("/api/source")
def source(path: str, user: str, country: str = ""):
    """원본 문서·데이터 보기(읽기 전용). 등록된 경로만, 역할의 문서 등급(doc_classes) 안에서만 연다."""
    s = next((x for x in _sources(T) if x["path"] == path), None)
    if not s:
        raise KeyError(f"설정에 등록되지 않은 자료: {path}")
    role = T.policy()["roles"].get(T.role_of(user)) or {}
    if s["class"] not in role.get("doc_classes", []):
        T.q("INSERT INTO audit(ts,actor,via,action,resource,decision,reason) VALUES(?,?,?,?,?,?,?)",
            (core.now(), user, None, "source.view", path, "금지", f"문서 등급 '{s['class']}' 열람 권한 없음"))
        raise PermissionError(f"'{user}'는 '{s['class']}' 등급 자료를 열 수 없음: {s['name']}")
    lines = ((core.ROOT / "data_ext" if s["ext"] else T.data_root) / path).read_text(encoding="utf-8-sig").splitlines()
    out = {**s, "kind": "참가자 자료(data_ext)" if s["ext"] else "원본 기록(읽기 전용)", "total_lines": len(lines), "lines": lines[:500], "nos": list(range(1, min(len(lines), 500) + 1))}
    if country and path.endswith(".csv") and lines:
        f = _country_rows(path, country, lines)
        if f is None:
            out["filter"] = f"이 자료에는 국가를 가를 키가 없어 전체를 보여 줍니다({country} 전용 행 없음)"
        else:
            nos = f[0][:500]
            out.update(lines=[lines[0]] + [lines[n - 1] for n in nos], nos=[1] + nos, matched=len(f[0]), filter=f"{country} 해당 {len(f[0]):,}행 · 기준: {f[1]}")
    return out


@app.get("/api/policy")
def policy_get():
    return {"policy": T.policy(), "source": "관리자 변경본" if T.get("policy") else "설정 파일 기본값", "hard_forbidden": sorted(core.HARD_FORBIDDEN)}


@app.post("/api/policy")
def policy_set(b: dict = Body(...)):
    T.set_policy(b["user"], b["policy"])
    return policy_get()


@app.get("/api/audit")
def audit(user: str, only_blocked: bool = False):
    T.require(user, "audit.view")
    return T.q("SELECT * FROM audit" + (" WHERE decision!='허용'" if only_blocked else "") + " ORDER BY id DESC LIMIT 200")


@app.get("/api/reports/{rid}/pdf")
def report_pdf(rid: int):
    r = T.q("SELECT pdf_path, body FROM reports WHERE id=?", (rid,))
    if not r or not r[0]["pdf_path"]:
        raise KeyError("PDF가 생성되지 않은 보고서")
    if not os.path.exists(r[0]["pdf_path"]):  # 배포 환경은 재시작하면 서버 폴더가 비워진다 → 저장된 보고 내용으로 PDF를 다시 만든다
        from . import report
        report._pdf(T, rid, json.loads(r[0]["body"]))
        r = T.q("SELECT pdf_path FROM reports WHERE id=?", (rid,))
    return FileResponse(r[0]["pdf_path"], media_type="application/pdf", filename=os.path.basename(r[0]["pdf_path"]))
