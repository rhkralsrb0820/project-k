"""A/B 실험 등록 → 점검 → 재무·승인 게이트 → 승인 → 진행 → 성과 입력 → 분석 → 종결 → 다음 실험.
외부 게시·광고 집행·지출은 하지 않는다. '진행'은 고객사가 직접 집행했다는 가상 입력 표시다."""
import csv, io, json, math
from . import finance, supply
from .core import now, dumps, ALLOW, NEED_APPROVAL

REQUIRED = {"country": "국가", "sku_id": "대상 상품(SKU)", "target": "대상 고객", "objective": "실험 목적", "hypothesis": "가설", "variant_a": "A안(비교군)",
            "variant_b": "B안(실험군)", "assignment": "실험군·비교군 배정 방식", "channel": "채널", "start_date": "시작일", "end_date": "종료일",
            "primary_metric": "핵심 지표", "success_value": "성공 기준", "stop_value": "중단 기준", "budget_krw": "예산",
            "cash_out_date": "현금 집행 예정일", "ops_conditions": "배송·통관·현지 운영 조건", "owner": "담당자"}
OPTIONAL = ["secondary_metrics", "decision_deadline", "proposal_total_krw", "min_orders_per_arm", "notes", "suggestions"]
METRICS = {"ctr": ("CTR = clicks / impressions", True), "cvr": ("CVR = orders / clicks", True),
           "nominal_roas": ("명목 ROAS = GMV / 광고비", True), "real_roas": ("실질 ROAS = 공헌이익 / 광고비", True),
           "return_rate": ("반품률 = 환불액 / GMV", False)}  # (정의, 높을수록 좋은가)
OBS_FIELDS = ["impressions", "clicks", "orders", "gmv_krw", "refund_krw", "ad_cost_krw", "product_cost_krw",
              "channel_fee_krw", "shipping_krw", "return_logistics_krw"]
OPEN_GATE = ("재무 확인 필요", "정책 확인 필요", "승인 대기", "승인됨")
HOLD = ("정보 부족", "대안 검토 필요")  # 대안 검토 필요 = SCM이 공급 불가로 회신한 상품(A-014)


def _row(t, exp_id):
    r = t.q("SELECT * FROM experiments WHERE id=?", (exp_id,))
    if not r:
        raise KeyError(f"실험 {exp_id} 없음")
    e = r[0]
    for k in ("data", "gate", "analysis"):
        e[k] = json.loads(e[k]) if e[k] else None
    e["finance_confirmed_scenario"] = (e["data"] or {}).get("_finance_confirmed_scenario")
    return e


def get(t, exp_id):
    e = _row(t, exp_id)
    e["missing"] = missing(t, e["data"])
    e["history"] = t.q("SELECT ts,actor,event,detail FROM history WHERE exp_id=? ORDER BY id", (exp_id,))
    e["observations"] = [{**json.loads(o["data"]), "id": o["id"], "arm": o["arm"], "source": o["source"]}
                         for o in t.q("SELECT * FROM observations WHERE exp_id=? ORDER BY id", (exp_id,))]
    e["todos"] = t.q("SELECT * FROM todos WHERE exp_id=? ORDER BY id", (exp_id,))
    e["supply"] = supply.check(t, e["data"])
    return e


def list_all(t):
    return [get(t, r["id"]) for r in t.q("SELECT id FROM experiments ORDER BY id DESC")]


def missing(t, d):
    m = [label for k, label in REQUIRED.items() if d.get(k) in (None, "", [])]
    if d.get("country") and d["country"] not in t.cfg["countries"]:
        m.append(f"국가 '{d['country']}'는 고객사 설정에 없음")
    if d.get("primary_metric") and d["primary_metric"] not in METRICS:
        m.append(f"핵심 지표 '{d['primary_metric']}'는 지원 지표가 아님({', '.join(METRICS)})")
    if d.get("ops_conditions") and d.get("ops_status") != "확인됨":
        m.append("배송·통관·현지 운영 조건이 '미확인' 상태(해외 운영 확인 필요)")
    s = supply.check(t, d)
    if d.get("sku_id") and s is None:
        m.append(f"대상 상품 '{d['sku_id']}'는 SKU 마스터(m_sku_ms)에 없음")
    elif s and s["message"]:
        m.append(s["message"])
    return m


def _hold(t, d):
    s = supply.check(t, d)
    return "대안 검토 필요" if s and s["state"] == "공급 불가" else "정보 부족"


def _owner(e):
    return e["data"].get("owner") or e["created_by"]


def _log(t, exp_id, actor, event, detail=""):
    t.q("INSERT INTO history(exp_id,ts,actor,event,detail) VALUES(?,?,?,?,?)", (exp_id, now(), actor, event, detail))


def _todo(t, exp_id, assignee, kind, title):
    if not t.q("SELECT 1 FROM todos WHERE exp_id=? AND kind=? AND assignee=? AND status='열림'", (exp_id, kind, assignee)):
        t.q("INSERT INTO todos(exp_id,assignee,kind,title,status,created_at) VALUES(?,?,?,?,?,?)", (exp_id, assignee, kind, title, "열림", now()))


def _close(t, exp_id, kinds, result):
    for k in kinds:
        t.q("UPDATE todos SET status='완료', closed_at=?, result=? WHERE exp_id=? AND kind=? AND status='열림'", (now(), result, exp_id, k))


def _save(t, e, status=None):
    t.q("UPDATE experiments SET status=?, data=?, gate=?, analysis=?, updated_at=? WHERE id=?",
        (status or e["status"], dumps(e["data"]), dumps(e["gate"]) if e["gate"] else None,
         dumps(e["analysis"]) if e["analysis"] else None, now(), e["id"]))


def _clean(data):
    d = {k: v for k, v in data.items() if k in REQUIRED or k in OPTIONAL}  # '_' 내부 필드(승인 기록 등)는 입력으로 못 바꾼다
    for k in ("budget_krw", "proposal_total_krw", "min_orders_per_arm", "success_value", "stop_value"):
        if d.get(k) not in (None, ""):
            d[k] = float(d[k]) if k.endswith("_value") else int(float(d[k]))
    return d


def create(t, user, data, parent_id=None, via=None):
    t.require(user, "experiment.draft", via=via)
    d = _clean(data)
    d.setdefault("ops_status", "미확인")
    m = missing(t, d)
    st = _hold(t, d) if m else "초안"
    eid = t.q("INSERT INTO experiments(status,data,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
              (st, dumps(d), parent_id, user, now(), now()))
    _log(t, eid, user, "등록", f"상태 {st}" + (f" · 누락 {len(m)}건" if m else ""))
    return get(t, eid)


def update(t, user, exp_id, data):
    t.require(user, "experiment.draft", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] in ("진행 중", "결과 분석 완료", "종료"):
        raise ValueError(f"'{e['status']}' 상태에서는 설계를 바꿀 수 없음(다음 실험으로 등록)")
    before = dict(e["data"])
    e["data"].update(_clean(data))
    changed = [k for k in e["data"] if e["data"].get(k) != before.get(k)]
    m = missing(t, e["data"])
    _log(t, exp_id, user, "수정", ", ".join(changed))
    if e["status"] in OPEN_GATE and not m:  # 조건 변경 → 기존 판단·승인을 그대로 두지 않고 재검증
        if e["data"].pop("_approval", None):
            _log(t, exp_id, "system", "승인 효력 정지", "승인 후 설계가 바뀌어 재승인 필요")
        return _apply_gate(t, e, user, f"설계 변경({', '.join(changed)})")
    if e["status"] in OPEN_GATE or e["status"] in ("초안", *HOLD, "반려"):
        st = _hold(t, e["data"]) if m else ("반려" if e["status"] == "반려" else "초안")
        if e["status"] in OPEN_GATE:
            e["gate"] = None
            e["data"].pop("_approval", None)
            _close(t, exp_id, ["재무 확인", "정책 확인", "승인", "집행 준비", "운영 준비"], "설계 변경으로 보류 — 취소")
        _save(t, e, st)
    return get(t, exp_id)


def ops_confirm(t, user, exp_id, note):
    """해외 운영[교육용 가정]의 운영조건 회신(가상 입력)."""
    t.require(user, "ops.confirm", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    e["data"]["ops_status"] = "확인됨"
    e["data"]["ops_conditions"] = (e["data"].get("ops_conditions") or "") + f" / 해외 운영 회신(가상 입력): {note}"
    st = e["status"]
    if st == "정보 부족" and not missing(t, e["data"]):
        st = "초안"
    _save(t, e, st)
    _close(t, exp_id, ["운영조건 확인"], note)
    _log(t, exp_id, user, "운영조건 확인(가상 입력)", note)
    return get(t, exp_id)


def supply_confirm(t, user, exp_id, available, note):
    """SCM·재고 담당[교육용 가정]의 공급 가능 여부 회신(가상 입력). 승인이 아니다.
    회신은 상품 단위로 저장되어 같은 상품의 다른 실험과 이후 등록되는 실험 판단에도 쓰인다."""
    t.require(user, "supply.confirm", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    sku = e["data"].get("sku_id")
    if not supply.check(t, e["data"]):
        raise ValueError("대상 상품이 없거나 SKU 마스터에 없음")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("공급 가능 여부의 근거(물량·입고 시점 등)를 입력해야 함")
    supply.set_reply(t, user, sku, bool(available), note.strip(), exp_id)
    detail = f"{sku} SCM 회신(가상 입력): {'공급 가능' if available else '공급 불가'} — {note.strip()}"
    for r in t.q("SELECT id FROM experiments WHERE status NOT IN ('결과 분석 완료','종료') ORDER BY id"):
        x = _row(t, r["id"])
        if x["data"].get("sku_id") != sku:
            continue
        _log(t, x["id"], user, "공급 회신 반영", detail)
        _close(t, x["id"], ["공급 확인"], detail)
        m = missing(t, x["data"])
        if x["status"] == "진행 중":
            if not available:  # 진행 중단은 사람이 결정한다
                _todo(t, x["id"], _owner(x), "중단 검토", f"실험 #{x['id']} 진행 중 {detail} — 중단·축소 여부 판단")
        elif not available:
            if x["status"] in OPEN_GATE:
                if x["data"].pop("_approval", None):
                    _log(t, x["id"], "system", "승인 효력 정지", "공급 불가 회신 — 기존 승인으로 진행할 수 없음")
                x["gate"] = None
                _close(t, x["id"], ["재무 확인", "정책 확인", "승인", "집행 준비", "운영 준비"], detail)
            _todo(t, x["id"], _owner(x), "대안 검토", f"실험 #{x['id']} {supply.check(t, x['data'])['message'][4:]}")
            _save(t, x, "대안 검토 필요")
        elif x["status"] in HOLD:
            _close(t, x["id"], ["대안 검토"], detail)
            _save(t, x, _hold(t, x["data"]) if m else "초안")
    return get(t, exp_id)


def _apply_gate(t, e, actor, reason):
    g = finance.review(t, e, via="orchestrator")
    old = (e["gate"] or {}).get("status"), (e["gate"] or {}).get("approver")
    e["gate"] = g
    _close(t, e["id"], ["재무 확인", "정책 확인", "승인", "집행 준비", "운영 준비"], f"재검토: {reason}")
    if g["status"] == "재무 확인 필요":
        _todo(t, e["id"], "fin_staff", "재무 확인", f"실험 #{e['id']} 집행일({e['data']['cash_out_date']})이 재전망 하회 구간(또는 가정을 바꾸면 늘어나는 구간)과 겹침 — 유입·유출 시점 확인 또는 입력 정정")
    elif g["status"] == "승인 대기":
        _todo(t, e["id"], g["approver"], "승인", f"실험 #{e['id']} 승인 검토(예산 {e['data']['budget_krw']:,}원, 집행 {e['data']['cash_out_date']})")
    elif g["status"] == "정책 확인 필요":
        _todo(t, e["id"], "정책 담당자(사용자 경유)", "정책 확인", "F11: CEO/이사회 승인 주체·순서·필요 증빙 확인. 회신 접수는 최종 승인이 아님")
    _save(t, e, g["status"])
    _log(t, e["id"], actor, "게이트 판정", f"{reason} → {g['status']}" + (f" · 승인자 {g['approver']}" if g["approver"] else "") +
         (f" (이전: {old[0]}/{old[1]})" if old[0] else "") + " · " + " / ".join(g["reasons"]))
    return get(t, e["id"])


def submit(t, user, exp_id):
    t.require(user, "experiment.submit", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] not in ("초안", *HOLD, "반려"):
        raise ValueError(f"'{e['status']}' 상태에서는 상신할 수 없음")
    m = missing(t, e["data"])
    if m:
        if any("운영 조건" in x for x in m):
            _todo(t, exp_id, "ops_staff", "운영조건 확인", f"실험 #{exp_id} 배송·통관·현지 운영 조건 확인 요청")
        s = supply.check(t, e["data"])
        if s and s["state"] == "공급 확인 필요":
            _todo(t, exp_id, "scm_staff", "공급 확인", f"실험 #{exp_id} {s['message'][4:]}")
        if s and s["state"] == "공급 불가":
            _todo(t, exp_id, _owner(e), "대안 검토", f"실험 #{exp_id} {s['message'][4:]}")
        rest = [x for x in m if "'미확인' 상태" not in x and not x.startswith("공급:")]  # 다른 부서 회신 대기 항목은 담당자 보완 업무에 넣지 않는다
        if rest:
            _todo(t, exp_id, _owner(e), "정보 보완", f"실험 #{exp_id} 누락 항목 보완: {', '.join(rest)}")
        _save(t, e, _hold(t, e["data"]))
        _log(t, exp_id, user, "상신 보류", "누락: " + ", ".join(m))
        return get(t, exp_id)
    _close(t, exp_id, ["정보 보완", "설계 수정", "대안 검토"], "상신")
    return _apply_gate(t, e, user, "상신")


def regate(t, exp_id, reason):
    e = _row(t, exp_id)
    if e["status"] not in OPEN_GATE:
        return get(t, exp_id)
    was_approved = e["status"] == "승인됨"
    old = e["gate"] or {}
    g = finance.review(t, e, via="orchestrator")
    if was_approved and (g["status"], g["approver"]) == ("승인 대기", old.get("approver")):
        _log(t, exp_id, "system", "게이트 재검증", f"{reason} → 승인 경로 변동 없음, 승인 유지")
        return get(t, exp_id)
    if was_approved:
        e["data"].pop("_approval", None)
        _log(t, exp_id, "system", "승인 효력 정지", f"{reason} → 승인 경로가 달라져 기존 승인({old.get('approver')})으로는 진행할 수 없음")
    return _apply_gate(t, e, "system", reason)


def regate_all(t, reason):
    return [{"id": r["id"], "status": regate(t, r["id"], reason)["status"]}
            for r in t.q("SELECT id FROM experiments WHERE status IN (?,?,?,?)", OPEN_GATE)]


def set_finance_scenario(t, user, overlay, label, reason):
    """재무 담당의 입력 정정·조건 변경(가상 입력). 기준 재전망은 그대로 두고 대안 시나리오로만 얹는다(G3-8).
    바뀌면 게이트가 열려 있는 모든 실험을 다시 판정한다."""
    t.require(user, "finance.scenario")
    if overlay and not (label and reason):
        raise ValueError("시나리오 이름과 근거를 입력해야 함")
    t.set(finance.SCENARIO_KEY, {"overlay": overlay or {}, "label": label if overlay else None, "reason": reason, "by": user,
                                 "at": now(), "as_of": t.as_of, "kind": "참가자 가정·가상 입력(대안 시나리오)"}, user)
    return regate_all(t, f"재무 시나리오 변경 → {label if overlay else finance.BASE_LABEL} ({reason or '기준 복귀'})")


def finance_confirm(t, user, exp_id, note):
    t.require(user, "finance.confirm", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] != "재무 확인 필요":
        raise ValueError("재무 확인 대기 상태가 아님")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("재무 담당의 유입·유출 시점 확인 내용과 근거를 입력해야 함")
    e["data"]["_finance_confirmed_scenario"] = e["gate"]["scenario"]
    _save(t, e)
    _log(t, exp_id, user, "재무 확인", f"하회 구간 확인({e['gate']['scenario']}): {note}")
    return _apply_gate(t, _row(t, exp_id), user, "재무 담당 구간 확인")


def decide(t, user, exp_id, approve, reason):
    t.require(user, "approval.decide", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] != "승인 대기":
        raise ValueError("승인 대기 상태가 아님")
    if t.role_of(user) != e["gate"]["approver"]:
        t.q("INSERT INTO audit(ts,actor,via,action,resource,decision,reason) VALUES(?,?,?,?,?,?,?)",
            (now(), user, None, "approval.decide", f"experiment:{exp_id}", "금지", f"승인권자는 {e['gate']['approver']}"))
        raise PermissionError(f"이 건의 승인권자는 {e['gate']['approver']}입니다({user}는 결정할 수 없음)")
    if not reason:
        raise ValueError("결정 사유를 입력해야 함")
    if approve and (e["gate"].get("in_window") or e["gate"].get("approver") == "group_ceo"):
        raise PermissionError("F11 정책 확인 필요: CEO/이사회 절차 미확인 — 최종 승인 불가")
    if approve:
        e["data"]["_approval"] = {"by": user, "at": now(), "as_of": t.as_of, "reason": reason, "approver_role": e["gate"]["approver"]}
        _save(t, e, "승인됨")
        _close(t, exp_id, ["승인"], f"승인: {reason}")
        if e["gate"].get("group_report"):
            _todo(t, exp_id, "mkt_staff", "그룹 보고", f"실험 #{exp_id} 승인 건 그룹 보고(모의 처리 — 외부 발송 없음)")
        d = e["data"]  # 승인 결과에 따른 후속 작업
        _todo(t, exp_id, _owner(e), "집행 준비", f"실험 #{exp_id} 승인됨 — {d['start_date']} 시작 준비 후 진행 시작 표시(실제 집행은 고객사가 직접 수행)")
        _todo(t, exp_id, "ops_staff", "운영 준비", f"실험 #{exp_id} 승인됨 — {d['country']} {d['channel']} · {d['sku_id']} 배송·통관·반품 접수 준비")
        _log(t, exp_id, user, "승인", reason)
    else:
        e["data"].pop("_approval", None)
        _save(t, e, "반려")
        _close(t, exp_id, ["승인"], f"반려: {reason}")
        _todo(t, exp_id, _owner(e), "설계 수정", f"실험 #{exp_id} 반려 — 사유 반영 후 재상신: {reason}")
        _log(t, exp_id, user, "반려", reason)
    return get(t, exp_id)


def start(t, user, exp_id):
    d = t.check(user, "experiment.start", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    approved = e["status"] == "승인됨" and e["data"].get("_approval")
    if approved and ((e["gate"] or {}).get("in_window") or (e["gate"] or {}).get("approver") == "group_ceo"):
        raise PermissionError("F11 정책 확인 필요: 이전 승인 기록만으로 진행 불가")
    if d["decision"] not in (ALLOW, NEED_APPROVAL) or not approved:
        _log(t, exp_id, user, "진행 차단", f"승인 없음(현재 {e['status']}) · 정책: {d['decision']}")
        raise PermissionError(f"승인되지 않은 실험은 진행할 수 없음(현재 상태: {e['status']})")
    _save(t, e, "진행 중")
    _close(t, exp_id, ["집행 준비", "운영 준비"], "진행 시작")
    _todo(t, exp_id, _owner(e), "성과 입력", f"실험 #{exp_id} 진행 중 — 종료일 {e['data']['end_date']} 이후 A·B안 성과 입력")
    _log(t, exp_id, user, "진행 시작(가상 입력)", "실제 게시·광고 집행은 고객사가 직접 수행. 시스템은 외부 실행을 하지 않음")
    return get(t, exp_id)


def _obs_clean(row):
    arm = str(row.get("arm", "")).strip().upper()
    if arm not in ("A", "B"):
        raise ValueError(f"arm은 A 또는 B여야 함: {row.get('arm')!r}")
    out = {"period_start": row.get("period_start") or None, "period_end": row.get("period_end") or None}
    for f in OBS_FIELDS:
        v = row.get(f)
        if v in (None, "", "NONE"):
            out[f] = None  # 결측은 0으로 바꾸지 않는다
        else:
            x = float(v)
            if not math.isfinite(x) or x < 0:
                raise ValueError(f"{f}는 음수일 수 없음: {v}")
            out[f] = x
    return arm, out


def add_observations(t, user, exp_id, rows, source="직접 입력"):
    t.require(user, "observation.input", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] not in ("진행 중", "결과 분석 완료"):
        raise ValueError(f"'{e['status']}' 상태에서는 성과를 입력할 수 없음(승인·진행 후 입력)")
    cleaned = [_obs_clean(r) for r in rows]  # 전부 검증한 뒤 저장
    kind = t.cfg["experiment_defaults"]["observation_kind"]
    for arm, o in cleaned:
        t.q("INSERT INTO observations(exp_id,arm,data,source,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (exp_id, arm, dumps(o), f"{source} · {kind}", user, now()))
    _log(t, exp_id, user, "성과 입력", f"{len(cleaned)}행 ({source})")
    _close(t, exp_id, ["성과 입력"], f"{len(cleaned)}행 입력")
    if cleaned and e["analysis"]:
        _log(t, exp_id, user, "분석 효력 정지", dumps({"before": e["analysis"], "after": None,
             "reason": "성과 추가로 재분석 필요", "reports": "기존 보고서는 당시 기록이며 수동 재검토 필요"}))
        e["analysis"] = None
        _save(t, e, "진행 중")
        _close(t, exp_id, ["담당자 판단"], "성과 추가로 분석 효력 정지")
        _todo(t, exp_id, _owner(e), "결과 재검토", "성과 변경: 재분석 후 기존 보고서와 담당자 판단을 재검토하세요")
    return get(t, exp_id)


def import_csv(t, user, exp_id, text):
    """CSV(쉼표 또는 파이프) 가져오기. 엑셀은 CSV로 저장해 가져온다(xlsx 직접 읽기 미구현)."""
    head = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter="|" if "|" in head else ","))
    if not rows or "arm" not in rows[0]:
        raise ValueError("헤더에 arm 컬럼이 필요함. 사용 가능 컬럼: arm, period_start, period_end, " + ", ".join(OBS_FIELDS))
    return add_observations(t, user, exp_id, rows, "CSV 가져오기")


def _arm_metrics(rows):
    s, miss = {}, []
    for f in OBS_FIELDS:
        vals = [r[f] for r in rows]
        if any(v is None for v in vals):
            s[f] = None
            miss.append(f)
        else:
            s[f] = sum(vals)
    div = lambda a, b: None if a is None or b is None or b == 0 else a / b
    net = None if s["gmv_krw"] is None or s["refund_krw"] is None else s["gmv_krw"] - s["refund_krw"]
    costs = [s[k] for k in ("product_cost_krw", "ad_cost_krw", "channel_fee_krw", "shipping_krw", "return_logistics_krw")]
    contrib = None if net is None or any(c is None for c in costs) else net - sum(costs)
    starts, ends = [r["period_start"] for r in rows if r["period_start"]], [r["period_end"] for r in rows if r["period_end"]]
    return {"sums": s, "missing_fields": miss, "net_revenue": net, "contribution": contrib,
            "period": [min(starts) if starts else None, max(ends) if ends else None],
            "ctr": div(s["clicks"], s["impressions"]), "cvr": div(s["orders"], s["clicks"]),
            "nominal_roas": div(s["gmv_krw"], s["ad_cost_krw"]), "real_roas": div(contrib, s["ad_cost_krw"]),
            "return_rate": div(s["refund_krw"], s["gmv_krw"])}


def _ztest(x1, n1, x2, n2):
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return None if se == 0 else math.erfc(abs((x2 / n2 - x1 / n1) / se) / math.sqrt(2))


def analyze(t, exp_id, actor="analyst", via=None):
    """기준 대비 계산만 한다. 예산 확대·중단 결정은 사람이 한다."""
    t.require(actor, "experiment.analyze", f"experiment:{exp_id}", via)
    e = get(t, exp_id)
    d, df = e["data"], t.cfg["experiment_defaults"]
    arms = {a: [o for o in e["observations"] if o["arm"] == a] for a in "AB"}
    res = {"as_of": t.as_of, "kind": "계산 결과(입력 관측값 기반)", "holds": [], "warnings": [], "arms": {}, "verdict": None,
           "primary_metric": d.get("primary_metric"), "definition": METRICS.get(d.get("primary_metric"), ("", True))[0],
           "note": "입력된 관측값이 합성·가상 입력이면 결과도 그 가정 아래의 모의 결과다. 인과효과를 단정하지 않는다."}
    for a in "AB":
        if not arms[a]:
            res["holds"].append(f"{a}안 성과 데이터 없음")
        else:
            res["arms"][a] = _arm_metrics(arms[a])
    pm = d.get("primary_metric")
    for field in ("assignment", "variant_a", "variant_b", "success_value", "stop_value"):
        if d.get(field) in (None, ""):
            res["holds"].append(f"기준 설정 필요: {REQUIRED[field]}")
    if pm not in METRICS:
        res["holds"].append("기준 설정 필요: 지원되는 핵심 지표")
    if len(res["arms"]) == 2:
        A, B = res["arms"]["A"], res["arms"]["B"]
        for a, m in res["arms"].items():
            if m["missing_fields"]:
                res["warnings"].append(f"{a}안 결측: {', '.join(m['missing_fields'])}")
            if m.get(pm) is None:
                res["holds"].append(f"{a}안 {pm} 계산 불가(결측 또는 분모 0)")
            if m["sums"]["orders"] is not None and m["sums"]["orders"] < (d.get("min_orders_per_arm") or df["min_orders_per_arm"]):
                res["holds"].append(f"{a}안 주문 {int(m['sums']['orders'])}건 < 최소 표본 {d.get('min_orders_per_arm') or df['min_orders_per_arm']}건")
            for k in ("refund_krw", "ad_cost_krw"):
                if m["sums"][k] is None:
                    res["warnings"].append(f"{a}안 {k} 미입력 — 반품·비용이 성과 검토에 반영되지 않음")
        if A["period"] != B["period"]:
            pr = lambda p: f"{p[0] or '미입력'} ~ {p[1] or '미입력'}"
            res["warnings"].append(f"비교 조건 차이: A안 기간 {pr(A['period'])} ≠ B안 기간 {pr(B['period'])} — 직접 비교 주의")
        if pm in ("ctr", "cvr") and A[pm] is not None and B[pm] is not None:
            num, den = ("clicks", "impressions") if pm == "ctr" else ("orders", "clicks")
            p = _ztest(A["sums"][num], A["sums"][den], B["sums"][num], B["sums"][den])
            res["p_value"] = p
            if p is None or p >= df["significance_alpha"]:
                res["holds"].append(f"A·B의 {pm} 차이가 통계적으로 구분되지 않음(p={'계산 불가' if p is None else f'{p:.3f}'} ≥ {df['significance_alpha']})")
        elif pm in ("nominal_roas", "real_roas", "return_rate"):
            res["warnings"].append(f"{pm}는 주문 단위 분산 자료가 없어 유의성 검정을 하지 않음 — 차이의 크기만 제시")
    if res["holds"]:
        res["verdict"] = "판단 보류"
    else:
        b, better = res["arms"]["B"][pm], METRICS[pm][1]
        ok = b >= d["success_value"] if better else b <= d["success_value"]
        stop = b <= d["stop_value"] if better else b >= d["stop_value"]
        res["verdict"] = "중단 기준 해당" if stop else "성공 기준 충족" if ok else "기준 미충족(성공·중단 사이)"
        res["criteria"] = {"success_value": d["success_value"], "stop_value": d["stop_value"], "b_value": b, "a_value": res["arms"]["A"][pm]}
    e2 = _row(t, exp_id)
    e2["analysis"] = res
    _save(t, e2, "결과 분석 완료" if e2["status"] in ("진행 중", "결과 분석 완료") else e2["status"])
    _log(t, exp_id, actor, "성과 분석", f"{res['verdict']}" + (f" · 보류 사유 {len(res['holds'])}건" if res["holds"] else ""))
    if e2["status"] in ("진행 중", "결과 분석 완료"):
        _todo(t, exp_id, _owner(e2), "담당자 판단", f"실험 #{exp_id} 분석 결과 '{res['verdict']}' — 확대·중단·재실험 판단 입력(시스템은 자동 확정하지 않음)")
    return res


def conclude(t, user, exp_id, judgment):
    t.require(user, "experiment.conclude", f"experiment:{exp_id}")
    e = _row(t, exp_id)
    if e["status"] != "결과 분석 완료":
        raise ValueError("분석이 끝난 실험만 종결할 수 있음")
    if not judgment:
        raise ValueError("담당자 판단을 입력해야 함")
    e["data"]["_human_judgment"] = {"by": user, "at": now(), "text": judgment}
    _save(t, e, "종료")
    _close(t, exp_id, ["담당자 판단", "결과 재검토", "중단 검토"], f"종결: {judgment}")
    _log(t, exp_id, user, "담당자 판단·종결", judgment)
    return get(t, exp_id)


def next_from(t, user, exp_id, changes):
    """이전 실험의 가설·결과·미해결 문제를 이어받아 다음 실험 초안을 만든다."""
    p = get(t, exp_id)
    if p["status"] not in ("결과 분석 완료", "종료"):
        raise ValueError("결과가 나온 실험에서만 다음 실험을 만들 수 있음")
    a = p["analysis"] or {}
    carry = {k: p["data"].get(k) for k in ("country", "sku_id", "target", "channel", "primary_metric", "assignment", "variant_a", "owner", "min_orders_per_arm")}
    carry["notes"] = (f"[이전 실험 #{exp_id}] 가설: {p['data'].get('hypothesis')} / 결과: {a.get('verdict')} / "
                      f"미해결: {'; '.join(a.get('holds', []) + a.get('warnings', [])) or '없음'} / "
                      f"담당자 판단: {(p['data'].get('_human_judgment') or {}).get('text', '미입력')}")
    carry.update(changes or {})
    return create(t, user, carry, parent_id=exp_id)
