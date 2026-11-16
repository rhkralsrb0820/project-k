"""총괄 최종 보고. 이번 실행에서 실제로 기록된 작업 결과(agent_tasks)와 현재 DB 상태만으로 만든다.
수행하지 않은 작업은 완료로 쓰지 않는다. 계산 수치와 규칙 기반 제안을 구분한다. PDF로 저장한다."""
import json, os
from pathlib import Path
from .core import ROOT, now, dumps

PCT = {"return_rate", "repurchase_90d"}
LBL = {"orders": "주문 수", "return_rate": "반품률", "nominal_roas": "명목 ROAS", "real_roas": "실질 ROAS", "repurchase_90d": "90일 재구매율"}


from .pdfdoc import disp  # noqa: F401  (표시 규칙은 pdfdoc 한 곳에 둔다)

SUB = "\n    · "


def sub(head, items):
    """여러 항목은 한 줄에 잇지 않고 항목마다 줄을 나눈다."""
    return head + SUB + SUB.join(str(x) for x in items)


def _fmt(k, v):
    if v is None:
        return "계산 불가"
    return f"{int(v):,}건" if k == "orders" else f"{v * 100:.1f}%" if k in PCT else f"{v:.2f}"


def _won(v):
    return f"{v / 1e8:,.1f}억원"


def _rstat(r):
    """확인 요청 상태. 접수된 회신이 모두 가상 입력이면 그 사실을 붙인다(실제 담당자 회신과 구분)."""
    h = r.get("history") or []
    return r["status"] + (f" (가상 회신 {len(h)}건 · 실제 담당자 회신 아님)" if h and all("가상" in x.get("mode", "") for x in h) else "")


def _view(t, run, out, todos, reqs, gaps, nxt, hum, src_sets):
    """PDF 양식에 넣을 구조화된 내용. 이미 계산·기록된 값만 옮긴다(새 계산 없음)."""
    from . import experiments as X, llm
    from .agents import WORKFLOWS
    val, fin, an, ret, des = (out.get(k) for k in ("validator", "finance", "analyst", "retriever", "designer"))
    exps = X.list_all(t)[::-1]
    n = lambda *st: sum(e["status"] in st for e in exps)
    HOLDS = ("정보 부족", "대안 검토 필요", "재무 확인 필요", "정책 확인 필요", "반려")
    w = (fin or {}).get("reforecast_windows") or []
    interp = json.loads(run["payload"]).get("_llm") or {}
    v = {"type": "plan" if des else "status", "goal": run["goal"], "requester": run["user"], "workflow_label": WORKFLOWS[run["workflow"]][0],
         "doc_no": f"실행 #{run['id']}", "authoring": llm.RULE_LABEL,
         "llm_note": (f"모델 제안: {interp.get('suggested_workflow') or '없음'} (실행은 선택한 작업 유형)" if interp.get("performed")
                      else "수행 안 함(LLM 미연결) · 선택한 작업 유형대로 실행")}
    v["kpis"] = [("등록된 실험", f"{len(exps)}건", f"승인 대기 {n('승인 대기')} · 보류 {n(*HOLDS)} · 진행 {n('진행 중')}"),
                 ("열린 작업", f"{len(todos)}건", "담당 역할별 처리 필요"),
                 ("현금 하회 예상 구간", (f"{w[0]['start'][5:]} ~ {w[0]['end'][5:]}".replace("-", ".") if w else "없음") if fin else "미계산", "재전망(계산 결과·가정 적용) · 종료일 미확정" if fin else "이번 실행에서 재무 검토 안 함"),
                 ("담당자 확인 요청", f"{len(reqs)}건", f"미회신 {sum(not r.get('history') for r in reqs)}건 · 회신은 승인이 아님")]
    risks = []
    if fin and fin["conflict"]:
        risks.append(f"공식 전망(부서 주장, 최저 {_won(fin['official_min'])})과 재전망(계산 결과, 최저 {_won(fin['reforecast_min']['balance'])})이 최소현금 기준({_won(t.cfg['finance']['covenant_min_krw'])})의 반대편에 있다. 어느 쪽도 확정되지 않았다(재무 확인 F3·F7).")
    if fin and fin.get("plans_in_window"):
        risks.append(f"집행 예정일이 재전망 하회 구간에 드는 캠페인 확대 기안 {fin['plans_in_window']}건. 이 구간의 승인 주체·순서·증빙은 원본에 없어 미확인이다(F11).")
    risks += [f"실험 #{e['id']}({e['data'].get('country') or '국가 미입력'}): {e['status']}" for e in exps if e["status"] in HOLDS][:5]
    if an and an["verdict"] == "판단 보류":
        risks.append(f"실험 #{an['exp_id']} 성과 분석: 판단 보류(사유 {len(an['holds'])}건)")
    if val and (val["linkage_failed"] or val["preliminary_mismatch"]):
        risks.append(f"데이터 검증 실패: 연결 검사 {len(val['linkage_failed'])}건, 예비 수치 불일치 {len(val['preliminary_mismatch'])}건")
    if any(r.get("history") for r in reqs):
        risks.append("담당자 확인 요청에 접수된 회신은 가상 회신(시험용 입력)이다. 실제 담당자 회신은 받지 않았다.")
    v["risks"] = risks or ["이번 실행 범위에서 확인된 위험 없음"]
    v["decide"], v["next"] = hum, nxt or ["없음"]

    if val:
        cs = val["baselines"]
        uni = {k: {ms[k]["status"] for ms in cs.values()} for k in LBL}
        v["perf"] = {"head": ["국가"] + [LBL[k] + (f"\n[{next(iter(uni[k]))}]" if len(uni[k]) == 1 else "") for k in LBL],
                     "rows": [[c] + [_fmt(k, ms[k]["value"]) + ("" if len(uni[k]) == 1 else f" [{ms[k]['status']}]") for k in LBL] for c, ms in cs.items()],
                     "notes": ["마케팅 주문·반품·광고비는 대표 표본이다. 표본 안의 지표만 계산하며 전체 시장·전사 성과로 일반화하지 않는다(데이터 제공자 확인 D8).",
                               "실질 ROAS는 주문당 1EA, 수수료는 환불 전 GMV 기준 등 가정(A-001~A-004)에 따른 예비 결과다. 명목 ROAS가 1을 넘어도 흑자를 뜻하지 않는다.",
                               "90일 재구매율은 관찰이 끝난 고객만으로 계산했다(A-005·A-006). 단위: 주문 수 건, 비율 %, ROAS 배."]}
    if an:
        ks = ("ctr", "cvr", "nominal_roas", "real_roas", "return_rate")
        v["analysis"] = {"exp_id": an["exp_id"], "verdict": an["verdict"], "head": ["안", "CTR", "CVR", "명목 ROAS", "실질 ROAS", "반품률", "관측 기간"],
                         "rows": [[a] + ["계산 불가" if an[a][k] is None else f"{an[a][k]:.4f}" for k in ks] + [f"{an[a]['period'][0] or '미입력'} ~ {an[a]['period'][1] or '미입력'}"] for a in "AB" if an.get(a)],
                         "notes": [f"핵심 지표: {an['primary_metric']}. 입력된 관측값(합성·가상 입력이면 모의 결과)의 계산 결과이며 인과효과를 단정하지 않는다."],
                         "holds": an["holds"] + an["warnings"]}
    if fin:
        below = fin["official_min"] < t.cfg["finance"]["covenant_min_krw"]
        v["fin"] = {"rows": [["정보 성격", "부서 주장·분석", "계산 결과(가정 적용)"],
                             ["전망 구간 최저 잔액", _won(fin["official_min"]), f"{_won(fin['reforecast_min']['balance'])} ({fin['reforecast_min']['date']})"],
                             ["최소현금 기준 미만 구간", "있음" if below else "없음(주말 잔액 기준)", "; ".join(f"{x['start']} ~ {x['end']}" for x in w) or "없음"],
                             ["집계 단위", "주 단위", "일 단위(AR·AP·발주 예정일 합산)"],
                             ["기준일·기간", f"{t.as_of} 이후 전망", f"{t.as_of} 이후 {t.cfg['finance']['horizon_days']}일"],
                             ["산정 근거", "원본에 없음 · 재무 담당 확인 필요(F7)", "calculation_definitions §1.2, 가정 A-007·A-008"],
                             ["확정 여부", "미확정", "미확정(구간 종료일은 확정 전망이 아님, F3)"],
                             ["포함 범위", "주별 유입·유출(구성 내역은 원본에 없음)", "기준일 현재 미수금·미지급금·발주대금의 예정분만(기준일 이후 신규 매출 회수·신규 지출 없음)"]],
                    "notes": [f"금액 단위: 원(억원 표기), VAT 제외. 최소현금 기준 {_won(t.cfg['finance']['covenant_min_krw'])}은 설정값(차입 약정 조건 문서)."] + ([(fin["scope_gap"]["message"])] if fin.get("scope_gap") else []),
                    "scope_facts": (fin.get("scope_gap") or {}).get("facts", []), "scope_candidates": (fin.get("scope_gap") or {}).get("candidates", []),
                    "unconfirmed": fin["unconfirmed"] + [f"기한 경과 미수·미지급 순액 {_won(fin['overdue_net'])}은 재전망에 넣지 않았다. 반영 시점은 원본에 없다."]
                    + ([f"전달받은 {fin['received_claim_check']['received']}와 재계산 {fin['received_claim_check']['computed']}가 다르다."]
                       if fin.get("received_claim_check") and fin["received_claim_check"]["result"] != "성공" else []),
                    "gate": fin.get("gate") and {"title": f"실험 #{fin['gate']['exp_id']} 승인 경로 판정: {fin['gate']['status']}" + (f" · 승인권자 {fin['gate']['approver']}" if fin["gate"]["approver"] else " · 승인권자 미정"),
                                                "reasons": fin["gate"]["reasons"]}}
    open_of = lambda e: "; ".join(f"{x['kind']}({x['assignee']})" for x in e["todos"] if x["status"] == "열림") or "—"
    v["exps"] = [[f"#{e['id']}", f"{e['data'].get('country') or '확인 필요'} / {e['data'].get('channel') or '확인 필요'}", e["data"].get("sku_id") or "확인 필요", e["status"],
                  (e["gate"] or {}).get("approver") or "미정", open_of(e)] for e in exps]
    v["todos"] = [[x["kind"], x["assignee"], x["title"]] for x in todos]
    v["requests"] = [[r["id"], r["owner"], _rstat(r), r.get("trigger") or "—"] for r in reqs]
    v["limits"] = gaps
    seen = set()
    v["sources"] = [list(k) for q in (ret or {}).get("queries", []) for h in q["hits"][:2]
                    if (k := (h["doc"], h["lines"], " ".join(h["text"].replace("*", "").replace("#", "").replace("`", "").split())[:110] + "…")) not in seen and not seen.add(k)]
    v["source_sets"] = src_sets
    v["files"] = [[f["name"], f"{f['size']:,}B", f["status"], "사용함" if f["used"] else "사용 안 함", f["note"]] for f in json.loads(run["payload"]).get("_files") or []]

    if des:
        e = X.get(t, des["exp_id"])
        d, sp, g, df = e["data"], e["supply"], (fin or {}).get("gate"), t.cfg["experiment_defaults"]
        kind = lambda k: "확인 필요(미입력)" if d.get(k) in (None, "") else ("설계 초안 · 담당자 확정 필요" if e["created_by"] == "designer" else "담당자 입력")
        val_ = lambda k: "확인 필요" if d.get(k) in (None, "") else (f"{int(d[k]):,}원" if k.endswith("_krw") else X.METRICS[d[k]][0] if k == "primary_metric" and d[k] in X.METRICS else d[k])
        row = lambda k, label=None: [label or X.REQUIRED[k], val_(k), kind(k)]
        base = (val or {}).get("baselines", {}).get(d.get("country") or "", {})
        miss = [k for k in X.REQUIRED if d.get(k) in (None, "")]
        st = e["status"]
        steps = [("설계 입력", "완료" if not miss else f"미입력 {len(miss)}항목"), ("운영 확인", d.get("ops_status") or "미확인"), ("공급 확인", sp["state"] if sp else "상품 미입력"),
                 ("상신", "완료" if e["gate"] or st in ("진행 중", "결과 분석 완료", "종료") else "대기"), ("재무·정책", e["gate"]["status"] if e["gate"] else "대기"),
                 ("승인", "승인됨" if d.get("_approval") else "반려" if st == "반려" else "대기"), ("진행", st if st in ("진행 중", "결과 분석 완료", "종료") else "대기")]
        done = {"완료", "확인됨", "충족", "공급 가능(회신)", "승인 대기", "승인됨"}
        cur = next((i for i, (_, x) in enumerate(steps) if x not in done), len(steps) - 1)
        v["plan"] = {
            "kpis": [("실험", f"#{e['id']}", f"상태: {st}"), ("대상 국가", d.get("country") or "확인 필요", f"채널: {d.get('channel') or '확인 필요'}"),
                     ("예산", val_("budget_krw"), f"현금 집행: {d.get('cash_out_date') or '확인 필요'}"), ("입력 필요", f"{len(e['missing'])}항목", "입력 전에는 상신할 수 없음")],
            "background": [f"요청: {run['goal']}"] + ([f"{d['country']} 기준선(과거 주문 표본의 관측값 · 실험 결과 아님): " + ", ".join(f"{LBL[k]} {_fmt(k, m['value'])} [{m['status']}]" for k, m in base.items())] if base else
                                                   ["대상 국가의 기준선을 이번 실행에서 계산하지 않았다."])
            + ["원본에는 실험군·비교군, 사전 가설, 노출 배정, 성공·중단 기준이 없다. 그래서 실험 설계 항목은 담당자가 정해야 한다."],
            "design": [row(k) for k in ("country", "channel", "target", "sku_id", "objective", "hypothesis", "variant_a", "variant_b", "assignment", "primary_metric", "success_value", "stop_value", "owner")],
            "suggestions": [f"{k}: {x}" for k, x in (d.get("suggestions") or {}).items() if x] or ["제안 없음"],
            "measure": [X.METRICS[d["primary_metric"]][0] if d.get("primary_metric") in X.METRICS else "핵심 지표: 확인 필요",
                        f"안(A/B)당 주문이 {d.get('min_orders_per_arm') or df['min_orders_per_arm']}건 미만이면 판단 보류",
                        f"CTR·CVR은 두 비율 검정(유의수준 {df['significance_alpha']}). ROAS·반품률은 주문 단위 자료가 없어 유의성 검정 없이 차이만 제시",
                        "결측과 분모 0은 '계산 불가', 한쪽 안의 자료가 없으면 '판단 보류'", "성과는 실행 뒤 담당자가 입력한다. 시스템은 예상 성과를 만들지 않는다."],
            "schedule": [row("start_date"), row("end_date"), row("budget_krw"), row("cash_out_date"), row("decision_deadline", "확정기한(있으면)")],
            "constraints": [
                ("재고·공급 (SCM·재고)", (sp["reasons"] + [f"판정: {sp['state']}"] + ([f"같은 제품군의 하한 이상 상품: {sp['alternatives_text']}"] if sp.get("alternatives_text") and sp["short"] else []))
                 if sp else ["대상 상품 미입력: 재고 커버와 안전재고 하한을 비교할 수 없다."], "원본 계산 + 참가자 가정 A-014" if sp else "확인 필요"),
                ("운영조건 (해외 운영)", [f"배송·통관·현지 운영 조건: {d.get('ops_conditions') or '확인 필요(미입력)'}", f"확인 상태: {d.get('ops_status') or '미확인'}",
                                  "국가별 배송 약속·통관·현지 운영비·반품 조건은 원본에 없다."], "교육용 가정"),
                ("현금 제약 (재무)", ([("재전망 하회 예상 구간: " + "; ".join(f"{x['start']} ~ {x['end']}" for x in w)) if w else "재전망 하회 예상 구간 없음"] + (g["reasons"] if g else [])) if fin
                 else ["이번 실행에서 재무 검토를 하지 않았다."], "계산 결과·가정 적용 · 미확정")],
            "flow": [(a, b, i == cur) for i, (a, b) in enumerate(steps)],
            "gate_title": (f"승인 경로 판정: {g['status']}" + (f" · 승인권자 {g['approver']}" if g["approver"] else " · 승인권자 미정")) if g else "승인 경로 판정: 미실시",
            "gate_reasons": (g["reasons"] if g else ["재무 검토 단계가 실행되지 않았다."]) + ["승인 규정은 원본 approval_authority_policy.md 그대로이며, 실험 집행안에 준용하는 것은 교육용 가정 A-009다."],
            "after": ["승인되면: 집행 준비(담당자), 운영 준비(해외 운영)", "진행을 시작하면: 성과 입력(담당자)", "분석이 끝나면: 담당자 판단 입력(확대·중단·재실험은 사람이 결정)",
                      "외부 게시·광고 집행·지급은 시스템이 하지 않는다."],
            "todos": [[x["kind"], x["assignee"], x["title"]] for x in e["todos"] if x["status"] == "열림"],
            "missing": e["missing"] or ["없음"]}
    return v


def build(t, run_id):
    run = t.q("SELECT * FROM runs WHERE id=?", (run_id,))[0]
    tasks = t.q("SELECT * FROM agent_tasks WHERE run_id=? ORDER BY seq", (run_id,))
    out = {x["role"]: json.loads(x["output"]) for x in tasks if x["output"]}
    from .agents import ROLES, NOT_IMPLEMENTED
    val, fin, an, cr, ret, des = (out.get(k) for k in ("validator", "finance", "analyst", "critic", "retriever", "designer"))
    todos = t.q("SELECT * FROM todos WHERE status='열림' ORDER BY id")
    exps = t.q("SELECT id,status,data FROM experiments ORDER BY id")
    S = []

    S.append(("1. 요청한 목표", [run["goal"], sub("요청 정보", [f"요청자: {run['user']}", f"작업 유형: {run['workflow']}", f"업무 기준일: {t.as_of}", f"실제 실행 시각: {run['started_at']}"])]))

    not_done = [x for x in tasks if x["status"] not in ("완료",) and x["role"] != "reporter"]
    S.append(("2. 현재 진행 상태", [f"{ROLES[x['role']]}: {'보고서 작성 중' if x['role'] == 'reporter' else x['status']}" + (f" — {x['error']}" if x["error"] else "") for x in tasks]
              + (["→ 완료되지 않은 단계가 있어 이 보고는 중간 현황이다."] if not_done else ["→ 보고서 작성 외 모든 단계가 완료됐다."])))

    S.append(("3. 완료된 작업", [f"[실제 실행{'' if x['status'] == '완료' else '·담당자 확인 남음'}] {ROLES[x['role']]}: {json.loads(x['output']).get('summary', '')}"
                           for x in tasks if x["status"] in ("완료", "사용자 확인 필요") and x["output"]] or ["없음"]))

    prog = [f"{ROLES[x['role']]}: {x['status']}" + (f" — {json.loads(x['output']).get('summary', '')}" if x["output"] else "") for x in not_done]
    prog += [f"실험 #{e['id']} ({json.loads(e['data']).get('country')}): {e['status']}" for e in exps if e["status"] not in ("종료",)]
    prog += [f"{ROLES[r]}: {why}" for r, why in NOT_IMPLEMENTED.items()]
    S.append(("4. 진행 중·미완료 작업", prog or ["없음"]))

    need = [f"[{x['assignee']}] {x['kind']}: {x['title']}" for x in todos]
    from .clarifications import list_requests
    reqs = list_requests(t, run_id)
    need += [f"[총괄 요청 {r['id']} / {r['owner']} / {_rstat(r)}] {r['question']} — 요청 사유: {r['trigger']}"
             + (" (사용자는 담당자 자료를 전달, 직접 정답 작성 불필요)" if r["reply"] else "") for r in reqs]
    if fin:
        need += [f"[확인 필요] {u}" for u in fin["unconfirmed"]]
    S.append(("5. 승인 또는 확인이 필요한 항목", need or ["없음"]))

    perf = []
    if val:
        perf.append("국가별 기준선 — 표본 안의 지표만 계산 가능. 전체 시장·전사 성과로 일반화 불가. 데이터 제공자에게 추출 모집단·기간·방식·누락 기준 확인 필요.")
        for c, ms in val["baselines"].items():
            perf.append(sub(c, [f"{LBL[k]} {_fmt(k, v['value'])} [{v['status']}]" for k, v in ms.items()]))
        perf.append("실질 ROAS는 주문당 1EA·수수료 환불 전 GMV 기준 등 가정(A-001~004)에 따른 예비 결과다. 명목 ROAS가 1을 넘어도 흑자를 뜻하지 않는다.")
    else:
        perf.append("국가별 기준선: 이번 실행에서는 계산하지 않음")
    if an:
        perf.append(f"실험 #{an['exp_id']} 분석[계산 결과]: {an['verdict']} (핵심 지표 {an['primary_metric']})")
        for a in "AB":
            if an.get(a):
                perf.append(sub(f"{a}안", [f"{k} {'계산 불가' if an[a][k] is None else f'{an[a][k]:.4f}'}" for k in ("ctr", "cvr", "nominal_roas", "real_roas", "return_rate")] + [f"기간 {an[a]['period'][0] or '미입력'} ~ {an[a]['period'][1] or '미입력'}"]))
    S.append(("6. 국가별·실험별 주요 성과", perf))

    gov = []
    if fin:
        w = fin["reforecast_windows"]
        gov.append(f"[부서 주장·분석] 재무팀 공식 전망: 전망 구간 최저 {_won(fin['official_min'])} (주 단위, 산정 근거 미확인)")
        gov.append("[계산 결과·가정 적용] 기준 재전망: 최소현금 기준 미만 구간 없음" if not w else sub("[계산 결과·가정 적용] 기준 재전망", [f"{x['start']} ~ {x['end']} 최소현금 기준 미만, 최저 {_won(x['min_balance'])}({x['min_date']})" for x in w]))
        if fin.get("scenario"):
            sw = fin["scenario"]["windows"]
            gov.append(f"[대안 시나리오·가상 입력] {fin['scenario']['label']}: " + ("최소현금 기준 미만 구간 없음" if not sw else "; ".join(f"{x['start']} ~ {x['end']}" for x in sw)))
        gov.append("두 전망은 서로 다르다. 시스템은 어느 쪽도 정답으로 확정하지 않았다." if fin["conflict"] else "공식 전망과 재전망의 하회 판정이 같다.")
        gov.append(f"기한 경과 미수·미지급 순액 {_won(fin['overdue_net'])}은 기준 재전망에 미반영(반영 시점 확인 필요)")
        if fin.get("gate"):
            g = fin["gate"]
            gov.append(sub(f"실험 #{g['exp_id']} 승인 경로 판정: {g['status']}" + (f" · 승인자 {g['approver']}" if g["approver"] else ""), g["reasons"]))
    else:
        gov.append("재무 검토: 이번 실행에서 수행되지 않음")
    pol = [json.loads(x["policy"]) for x in tasks if x["policy"]]
    gov.append(sub(f"정책 검사: 이번 실행 {len(pol)}건", [f"{d} {sum(p['decision'] == d for p in pol)}건" for d in ("허용", "승인 필요", "금지", "불명확")]))
    blocked = t.q("SELECT COUNT(*) n FROM audit WHERE decision!='허용'")[0]["n"]
    gov.append(sub("보안·거버넌스", [f"감사 로그 누적 차단·보류 기록: {blocked}건", f"외부 AI 전송: {'허용' if t.policy()['external_ai_transfer'] else '금지(기본값)'}", "외부 게시·광고 집행·지출·메시지 발송·삭제: 실행하지 않음"]))
    S.append(("7. 재무 제약 및 보안·거버넌스 검토 결과", gov))

    gaps = []
    if val:
        gaps.append(f"데이터 연결 검사 {val['linkage_checks']}건 중 실패 {len(val['linkage_failed'])}건" + (f": {val['linkage_failed']}" if val["linkage_failed"] else ""))
        gaps.append(f"전달받은 예비 수치 {val['preliminary_total']}건 중 원본 재현 불일치 {len(val['preliminary_mismatch'])}건")
        gaps += [f"[검증 불가] {u}" for u in val["unverifiable"]]
    if fin and fin.get("received_claim_check") and fin["received_claim_check"]["result"] != "성공":
        rc = fin["received_claim_check"]
        gaps.append(f"[불일치] {rc['received']} ↔ 원본 재계산 {rc['computed']}. {rc['note']}")
    if fin:
        gaps += [f"[전망 차이 후보 원인·계산] {x}" for x in fin["candidate_causes"]]
    if an:
        gaps += [f"[판단 보류 사유] {h}" for h in an["holds"]] + [f"[주의] {w}" for w in an["warnings"]]
    if cr:
        gaps += [f"[반론] {o}" for o in cr["objections"] if o.startswith(("[증거", "[해석", "[대표"))]
    if ret:
        gaps += [f"[근거 부족] 질의 '{q['query']}'에 맞는 문서 단락 없음" for q in ret["queries"] if q["insufficient"]]
        cf = sorted({c for q in ret["queries"] for c in q["conflicts"]})
        gaps += [f"[자료 충돌 {c['id']}] {c['text']}" for c in t.cfg.get("known_conflicts", []) if c["id"] in cf]
    gaps.append("적용 가정: data_ext/assumptions.csv (A-001 ~ A-016)")
    S.append(("8. 데이터 부족, 충돌, 오류 및 가정", gaps))

    src = [f"{h['doc']} {h['lines']}행 — “{h['text'][:70].replace(chr(10), ' ')}…”" for q in (ret or {}).get("queries", []) for h in q["hits"][:2]]
    src += [sub("지표", ["mkt_orders_ms", "mkt_returns_ms", "mkt_ad_spend_ms", "mkt_cost_master_ms", "mkt_repurchase_ms", "m_campaign_ms"])] if val else []
    src += [sub("현금", ["fin_cash_daily_ms", "fin_ar_aging_ms", "fin_ap_aging_ms", "sc_purchase_orders_ms", "fin_cash_forecast_ms", "calculation_definitions §1.2"])] if fin else []
    src += [f"[첨부·{'구조 확인에 사용' if f['used'] else '사용 안 함'}] {f['name']}: {f['note']}" for f in json.loads(run["payload"]).get("_files") or []]
    S.append(("9. 판단에 사용한 근거와 출처", src or ["이번 실행에서 수집된 근거 없음"]))

    nxt = []
    if fin and fin["conflict"]:
        nxt.append("재무팀에 공식 전망의 입력값·가정(발주대금, 바뀐 결제조건 반영 여부) 확인 요청")
    if des:
        nxt.append(f"실험 #{des['exp_id']}의 누락 항목을 담당자가 입력한 뒤 상신")
    if fin and fin.get("gate") and fin["gate"]["status"] == "재무 확인 필요":
        nxt.append("집행 예정일이 하회 예상 구간과 겹침 — 재무 담당의 구간 확인 후 그룹 CEO 상신 여부 결정, 또는 집행일 조정안 검토")
    if an and an["verdict"] == "판단 보류":
        nxt.append("보류 사유(표본·결측·조건 차이)를 해소할 추가 데이터 입력 또는 실험 기간 연장 검토")
    if an:
        nxt.append("결과 검토 후 다음 실험 생성(이전 가설·결과·미해결 문제 연결)")
    nxt += [f"열린 업무 처리: [{x['assignee']}] {x['title']}" for x in todos[:5]]
    S.append(("10. 제안하는 다음 행동 (규칙 기반 제안 — 계산 수치와 구분)", nxt or ["없음"]))

    hum = ["예산 확대·축소·중단 여부(시스템은 자동 결정하지 않음)", f"실험의 승인·반려(평시: 건별 {t.cfg['approval_rules']['single_limit_krw']:,}원 이하 및 기안 총액 {t.cfg['approval_rules']['proposal_total_limit_krw']:,}원 이하 팀장 전결 / 초과 대표 승인+그룹 보고 / 하회 구간 상위 승인, 주체·순서·증빙은 정책 담당자 확인 전 미확정)"]
    if fin and fin["conflict"]:
        hum.append("공식 전망과 재전망 중 어느 것을 판단 기준으로 쓸지")
    if an:
        hum.append(f"실험 #{an['exp_id']} 결과에 대한 담당자 최종 판단과 다음 실험 방향")
    S.append(("11. 사람이 최종 결정해야 하는 사항", hum))

    title = f"총괄 에이전트 현황 보고 — {run['goal'][:40]}"
    from . import llm
    view = _view(t, run, out, todos, reqs, gaps, nxt, hum, [x for x in src if x.startswith(("지표", "현금"))])
    text, _why, model = llm.draft(t, view["risks"] + [f"{a}: {b} ({c})" for a, b, c in view["kpis"]])  # 연결된 모델이 없으면 None
    view["ai_summary"], view["authoring"] = text, llm.authoring(model)
    body = {"view": view, "authoring": view["authoring"], "title": title, "run_id": run_id, "tenant": t.cfg["name"], "as_of": t.as_of, "created_at": now(), "run_status": "보고서 작성 시점: 진행 중",
            "sections": [{"title": a, "lines": b} for a, b in S],
            "notice": "LLM을 쓰지 않은 규칙 기반 보고다. 모든 수치는 원본 또는 입력값의 계산 결과이며, 제안은 10번 항목에만 있다."}
    rid = t.q("INSERT INTO reports(run_id,created_at,title,body) VALUES(?,?,?,?)", (run_id, body["created_at"], title, dumps(body)))
    return {"id": rid, "pdf_status": _pdf(t, rid, body)}


def finalize(t, report_id, run_status):
    """실행이 끝난 뒤 최종 실행 상태를 보고서에 반영하고 PDF를 다시 만든다. 2번 항목도 작성 시점이 아니라 최종 상태로 다시 쓴다."""
    from .agents import ROLES
    body = json.loads(t.q("SELECT body FROM reports WHERE id=?", (report_id,))[0]["body"])
    body["run_status"] = f"실행 최종 상태: {run_status}"
    tasks = t.q("SELECT * FROM agent_tasks WHERE run_id=? ORDER BY seq", (body["run_id"],))
    idle = [x for x in tasks if x["status"] in ("대기", "진행 중", "실패", "승인 대기")]
    body["sections"][1]["lines"] = [f"{ROLES[x['role']]}: {x['status']}" + (f" — {x['error']}" if x["error"] else "") for x in tasks] + [
        "→ 실행되지 않았거나 실패한 단계가 있다." if idle else "→ 모든 단계를 실행했다. '사용자 확인 필요'는 단계 실행은 끝났고 담당자 확인이 남았다는 뜻이다."]
    t.q("UPDATE reports SET body=? WHERE id=?", (dumps(body), report_id))
    _pdf(t, report_id, body)


def _pdf(t, rid, body):
    cfg = t.cfg["report"]
    font, bold = Path(os.environ.get("PROJECTK_FONT") or cfg["font_path"]), Path(os.environ.get("PROJECTK_FONT_BOLD") or cfg.get("font_bold_path", ""))
    if not font.exists():
        return f"환경 부족으로 미생성(글꼴 없음: {font})"
    try:
        from . import pdfdoc
    except ImportError:
        return "환경 부족으로 미생성(fpdf2 미설치)"
    from .agents import ROLES
    import logging
    logging.getLogger("fontTools").setLevel(logging.ERROR)  # 글꼴 서브셋 경고(MERG 테이블) 소음 제거
    tasks = [[x["seq"], ROLES[x["role"]], x["status"], json.loads(x["policy"])["decision"] if x["policy"] else "미실행",
              x["error"] or (json.loads(x["output"]).get("summary", "") if x["output"] else "—")]
             for x in t.q("SELECT * FROM agent_tasks WHERE run_id=? ORDER BY seq", (body["run_id"],))]
    out = getattr(t, "report_dir", ROOT / "reports" if t.db_path.name == f"{t.id}.db" else ROOT / "reports" / t.db_path.stem)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{t.id}_report_{rid:04d}.pdf"
    pages = pdfdoc.render(path, body, tasks, font, bold if bold.is_file() else font)
    t.q("UPDATE reports SET pdf_path=? WHERE id=?", (str(path), rid))
    body["pdf_pages"] = pages
    return "생성됨"
