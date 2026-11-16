"""총괄 오케스트레이터와 하위 역할. LLM을 쓰지 않는 규칙 기반 실행이다(외부 AI 전송 정책 기본값 = 금지).
각 단계는 실행 전에 정책 검사를 거치고, 실제 실행 기록(agent_tasks)을 남긴다.
총괄은 하위 역할을 via='orchestrator'로 호출하므로 자기 권한을 넘는 일을 시킬 수 없다."""
import json, traceback
from . import metrics, finance, experiments, rag, report, clarifications, llm, attachments
from .core import now, dumps, ALLOW, NEED_APPROVAL, DENY

ROLES = {"orchestrator": "총괄", "retriever": "자료 검색", "validator": "데이터 검증", "designer": "실험 설계", "analyst": "성과 분석",
         "finance": "재무 제약 검토", "critic": "결과 검증·반론", "voc": "고객 의견·설문 분석", "reporter": "보고서 작성"}
NOT_IMPLEMENTED = {"voc": "미구현 — 원본에 고객 의견·설문 데이터가 없음"}
ACTION = {"retriever": "rag.search", "validator": "metrics.compute", "designer": "experiment.draft", "analyst": "experiment.analyze",
          "finance": "finance.review", "critic": "critique.write", "reporter": "report.write"}
WORKFLOWS = {"status": ("현황 점검과 보고", ["retriever", "validator", "finance", "reporter"]),
             "design": ("해외 A/B 실험 설계", ["retriever", "validator", "designer", "finance", "reporter"]),
             "analyze": ("실험 성과 분석과 검증", ["analyst", "critic", "finance", "reporter"])}
VIA = "orchestrator"


def _retriever(c):
    p = c["payload"]
    qs = [q for q in [p.get("goal"), "covenant 하회 예상 구간 신규 현금 약정 승인", "캠페인 확대안 집행 승인 실질 공헌이익 기준",
                      "커머스 트랜잭션 대표 표본 절대금액 대사"] if q]
    res = [rag.search(c["t"], "retriever", q, VIA, k=3) for q in qs]
    return {"queries": [{"query": r["query"], "insufficient": r["insufficient"], "conflicts": [x["id"] for x in r["conflicts"]],
                         "hits": [{"doc": h["doc"], "lines": f"{h['line_start']}-{h['line_end']}", "text": h["text"][:160]} for h in r["hits"]]} for r in res],
            "summary": f"질의 {len(res)}건 중 근거 부족 {sum(r['insufficient'] for r in res)}건"}


def _validator(c):
    link, base = metrics.linkage(c["t"], "validator", VIA), metrics.baselines(c["t"], "validator", VIA)
    bad = [p for p in base["preliminary"] if p["result"] != "성공"]
    return {"linkage_failed": link["failed"], "linkage_checks": len(link["checks"]), "unverifiable": link["unverifiable"],
            "preliminary_total": len(base["preliminary"]), "preliminary_mismatch": bad,
            "baselines": {k: {m: {"value": v["value"], "status": v["status"]} for m, v in ms.items()} for k, ms in base["countries"].items()},
            "summary": f"연결 검사 {len(link['checks'])}건 중 실패 {len(link['failed'])}건 · 예비 수치 {len(base['preliminary'])}건 중 불일치 {len(bad)}건"}


def _designer(c):
    t, p = c["t"], c["payload"]
    country = p.get("country")
    b = (c["out"].get("validator") or {}).get("baselines", {}).get(country, {})
    rr = (b.get("real_roas") or {}).get("value")
    sug = {"근거": f"{country} 기준선(표본·관측값, 실험 결과 아님): 실질 ROAS {rr:.2f}(가정 적용·예비)" if rr is not None else "기준선 없음 — 제안 근거 부족",
           "핵심 지표": "real_roas — 승인 판단은 예상 GMV가 아니라 실질 공헌이익 기준(approval_authority_policy)",
           "성공 기준 제안": None if rr is None else f"B안 실질 ROAS ≥ {max(rr, 0):.2f} (현재 기준선 이상, 적자면 0 이상)",
           "중단 기준 제안": "B안 실질 ROAS ≤ 0 (공헌이익 적자)",
           "가설 제안": f"{country}에서 B안({p.get('change') or '변경 요소 입력 필요'})이 A안 대비 실질 ROAS를 개선한다",
           "주의": "규칙 기반 제안이다. 담당자가 확정해 직접 입력해야 기준으로 쓰인다. 기존 국가별 성과는 완료된 A/B 실험 결과가 아니다."}
    data = {"country": country, "objective": p.get("goal"), "variant_a": "현행 조건 유지(비교군)", "variant_b": p.get("change"),
            "primary_metric": "real_roas", "owner": c["user"], "suggestions": sug}
    e = experiments.create(t, "designer", data, via=VIA)
    c["exp_id"] = e["id"]
    return {"_status": "사용자 확인 필요" if e["missing"] else "완료", "exp_id": e["id"], "status": e["status"], "missing": e["missing"],
            "suggestions": sug, "summary": f"실험 #{e['id']} 초안 생성 · 담당자 입력 필요 {len(e['missing'])}항목"}


def _analyst(c):
    r = experiments.analyze(c["t"], c["payload"]["exp_id"], "analyst", VIA)
    c["exp_id"] = c["payload"]["exp_id"]
    fmt = lambda a: {k: r["arms"][a][k] for k in ("ctr", "cvr", "nominal_roas", "real_roas", "return_rate", "period", "missing_fields")} if a in r["arms"] else None
    return {"exp_id": c["exp_id"], "verdict": r["verdict"], "holds": r["holds"], "warnings": r["warnings"], "primary_metric": r["primary_metric"],
            "A": fmt("A"), "B": fmt("B"), "p_value": r.get("p_value"), "summary": f"실험 #{c['exp_id']}: {r['verdict']}"}


def _critic(c):
    a = c["out"].get("analyst") or {}
    e = experiments.get(c["t"], c["exp_id"])
    ob = [f"[보류 사유] {h}" for h in a.get("holds", [])] + [f"[주의] {w}" for w in a.get("warnings", [])]
    if any("합성" in o["source"] or "가상" in o["source"] for o in e["observations"]):
        ob.append("[증거 수준] 입력 관측값이 합성·가상 입력이다 — 실제 성과 증거가 아니며 해당 가정 아래의 모의 결과다")
    B = a.get("B") or {}
    if B.get("nominal_roas") and B["nominal_roas"] > 1 and B.get("real_roas") is not None and B["real_roas"] < 0:
        ob.append("[해석] B안 명목 ROAS는 1을 넘지만 실질 ROAS는 음수다 — 매출 기준으로 확대를 결정하면 안 된다")
    if a.get("verdict") == "성공 기준 충족" and e["data"].get("primary_metric") != "real_roas" and B.get("real_roas") is None:
        ob.append("[해석] 핵심 지표는 충족했지만 실질 ROAS를 계산할 비용 자료가 없다 — 승인 판단 기준(실질 공헌이익)을 충족했다고 볼 수 없다")
    ob.append("[대표성] 실험 대상의 국가·채널 대표성과 배정의 무작위성은 시스템이 검증하지 못한다 — 인과효과로 단정 불가")
    ob.append("[의사결정] 지표가 높거나 낮다는 이유만으로 예산 확대·중단을 자동 결정하지 않는다 — 담당자 판단 필요")
    return {"objections": ob, "summary": f"반론·한계 {len(ob)}건"}


def _finance(c):
    t = c["t"]
    cmp_ = finance.compare(t, "finance", VIA)
    out = {"conflict": cmp_["conflict"], "official_min": cmp_["official"]["min_closing"], "reforecast_windows": cmp_["reforecast"]["windows"],
           "reforecast_min": cmp_["reforecast"]["min"], "scenario": cmp_["scenario"] and {"label": cmp_["scenario"]["label"], "windows": cmp_["scenario"]["windows"]},
           "candidate_causes": cmp_["candidate_causes"], "scope_gap": cmp_["scope_gap"], "received_claim_check": cmp_["received_claim_check"],
           "overdue_net": cmp_["reforecast"]["inputs"]["overdue_net"], "unconfirmed": cmp_["policy_findings"]["unconfirmed"],
           "plans_in_window": cmp_["budget_plans"]["in_window_count"]}
    if c.get("exp_id"):
        g = finance.review(t, experiments.get(t, c["exp_id"]), "finance", VIA)
        out["gate"] = {"exp_id": c["exp_id"], "status": g["status"], "approver": g["approver"], "reasons": g["reasons"]}
    out["_status"] = "사용자 확인 필요" if cmp_["conflict"] else "완료"
    out["summary"] = ("공식 전망과 재전망 충돌 — 재무 확인 필요" if cmp_["conflict"] else "전망 충돌 없음") + (f" · 실험 #{c['exp_id']} 게이트: {out['gate']['status']}" if "gate" in out else "")
    return out


def _reporter(c):
    reqs = clarifications.request(c["t"], c["user"], c["run_id"], c["out"])
    r = report.build(c["t"], c["run_id"])
    return {"report_id": r["id"], "pdf": r["pdf_status"], "information_requests": [x["id"] for x in reqs],
            "summary": f"보고서 #{r['id']} 작성 · PDF: {r['pdf_status']} · 사용자에게 요청한 정보 {len(reqs)}건"}


STEPS = {"retriever": _retriever, "validator": _validator, "designer": _designer, "analyst": _analyst, "critic": _critic,
         "finance": _finance, "reporter": _reporter}


def run(t, user, workflow, payload):
    """총괄: 목표 → 단계 분해 → 순서대로 배정·실행 → 실패/차단 시 뒤 단계 중지 → 보고."""
    t.require(user, "run.start", workflow)
    if workflow not in WORKFLOWS:
        raise ValueError(f"지원하지 않는 작업: {workflow}")
    title, roles = WORKFLOWS[workflow]
    if workflow == "analyze" and not payload.get("exp_id"):
        raise ValueError("분석할 실험(exp_id)이 필요함")
    goal = payload.get("goal") or title
    # LLM 요청 해석(연결된 모델이 없으면 '수행 안 함'). 결과는 기록만 하고 실행할 작업은 바꾸지 않는다 — 실행은 선택한 작업 유형이다.
    payload = {**payload, "_llm": llm.interpret(t, payload.get("goal"), list(WORKFLOWS))}
    attachments.check_link(t, user, payload.get("attachment_ids"))  # 연결할 수 없는 첨부가 있으면 실행을 만들지 않는다
    run_id = t.q("INSERT INTO runs(workflow,goal,user,status,payload,started_at) VALUES(?,?,?,?,?,?)",
                 (workflow, goal, user, "진행 중", dumps(payload), now()))
    ids = [t.q("INSERT INTO agent_tasks(run_id,seq,role,title,status) VALUES(?,?,?,?,?)", (run_id, i, r, f"{ROLES[r]} — {title}", "대기"))
           for i, r in enumerate(roles, 1)]
    if payload.get("attachment_ids"):  # 첨부를 이 요청에 연결하고, 총괄이 실제로 읽은 파일과 읽지 못한 파일을 실행 기록에 남긴다
        attachments.link(t, user, payload["attachment_ids"], "run", run_id)
        payload = {**payload, "_files": attachments.use_in_run(t, run_id)}
        t.q("UPDATE runs SET payload=? WHERE id=?", (dumps(payload), run_id))
    c = {"t": t, "user": user, "payload": payload, "run_id": run_id, "out": {}, "exp_id": payload.get("exp_id")}
    halted, statuses = None, []
    for tid, role in zip(ids, roles):
        if halted and role != "reporter":  # 앞 단계가 막히면 의존 단계는 실행하지 않는다(보고만 수행)
            statuses.append("대기")
            continue
        pol = t.check(role, ACTION[role], f"run:{run_id}", via=VIA)
        if pol["decision"] != ALLOW:
            st = "승인 대기" if pol["decision"] == NEED_APPROVAL else "실패" if pol["decision"] == DENY else "사용자 확인 필요"
            t.q("UPDATE agent_tasks SET status=?, policy=?, ended_at=?, error=? WHERE id=?", (st, dumps(pol), now(), f"정책 검사: {pol['decision']} — {pol['reason']}", tid))
            halted = halted or f"{ROLES[role]} 단계 정책 검사 {pol['decision']}"
            statuses.append(st)
            continue
        t.q("UPDATE agent_tasks SET status='진행 중', policy=?, started_at=? WHERE id=?", (dumps(pol), now(), tid))
        try:
            out = STEPS[role](c)
            st = out.pop("_status", "완료")
            c["out"][role] = out
            t.q("UPDATE agent_tasks SET status=?, ended_at=?, output=? WHERE id=?", (st, now(), dumps(out), tid))
        except Exception as ex:  # 권한 차단(PermissionError) 포함 — 실패를 숨기지 않고 기록
            st = "실패"
            t.q("UPDATE agent_tasks SET status='실패', ended_at=?, error=? WHERE id=?", (now(), f"{type(ex).__name__}: {ex}", tid))
            halted = halted or f"{ROLES[role]} 단계 실패: {ex}"
            if not isinstance(ex, (PermissionError, ValueError, KeyError)):
                traceback.print_exc()
        statuses.append(st)
    final = ("실패" if "실패" in statuses else "승인 대기" if "승인 대기" in statuses else
             "사용자 확인 필요" if "사용자 확인 필요" in statuses else "완료")
    rep = (c["out"].get("reporter") or {}).get("report_id")
    t.q("UPDATE runs SET status=?, ended_at=?, report_id=? WHERE id=?", (final, now(), rep, run_id))
    if rep:
        report.finalize(t, rep, final)
    return get_run(t, run_id)


def get_run(t, run_id):
    r = t.q("SELECT * FROM runs WHERE id=?", (run_id,))[0]
    r["payload"] = json.loads(r["payload"])
    r["information_requests"] = clarifications.list_requests(t, run_id)
    r["tasks"] = t.q("SELECT * FROM agent_tasks WHERE run_id=? ORDER BY seq", (run_id,))
    for x in r["tasks"]:
        x["output"] = json.loads(x["output"]) if x["output"] else None
        x["policy"] = json.loads(x["policy"]) if x["policy"] else None
        x["role_label"] = ROLES[x["role"]]
    return r


def agent_board(t):
    """역할별 최신 작업 상태. 실행 기록이 없으면 '대기', 구현 안 된 역할은 그대로 '미구현'으로 표시한다."""
    board = []
    for role, label in ROLES.items():
        if role in NOT_IMPLEMENTED:
            board.append({"role": role, "label": label, "status": "미구현", "note": NOT_IMPLEMENTED[role]})
            continue
        if role == "orchestrator":
            r = t.q("SELECT status, goal AS title, ended_at FROM runs ORDER BY id DESC LIMIT 1")
        else:
            r = t.q("SELECT status, title, ended_at, error FROM agent_tasks WHERE role=? ORDER BY id DESC LIMIT 1", (role,))
        board.append({"role": role, "label": label, **(r[0] if r else {"status": "대기", "note": "실행 기록 없음"})})
    return board
