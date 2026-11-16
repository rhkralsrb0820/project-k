"""담당자 확인 요청과 사용자 경유 회신. 회신은 가상 입력이며 승인 증빙을 대체하지 않는다."""
from .core import now

REQUESTS = [
    ("F3", "재무 담당", "잔액 최저점 날짜는 하회 종료일이 아닙니다. 최저점 이후 집행도 검토하도록 실제 유입·유출일과 하회 종료 근거를 확인해 주세요.", "fin_cash_daily_ms / fin_ar_aging_ms / fin_ap_aging_ms / sc_purchase_orders_ms"),
    ("F7", "재무 담당", "공식 전망의 발주대금, AR 회수 조건(결제조건이 바뀐 거래처 포함), 연체 순액 반영 여부·금액·시점을 확인해 주세요. 공식 전망과 가정 기반 재전망을 함께 유지합니다.", "fin_cash_forecast_ms / 결제조건 변경 통보문"),
    ("F11", "정책 담당자", "CEO/이사회 승인 주체·순서·필요 증빙과 전망 충돌 시 확정 절차를 확인해 주세요. 시스템은 절차를 만들거나 최종 승인 완료로 처리하지 않습니다.", "00_common/approval_authority_policy.md / covenant_terms_ms.md"),
    ("D8", "데이터 제공자", "추출 모집단·기간·방식·누락 기준을 확인해 주세요. 표본 내 지표만 계산하며 전체 시장·전사 성과로 일반화하지 않습니다.", "mkt_orders_ms / mkt_returns_ms / mkt_ad_spend_ms"),
    ("T9", "자료·사용 권한 담당자", "학습 목적, 학습·평가 사용 권한, 자료 목록·원본 행 연결, 기본 모델과 평가 기준을 확인해 주세요. 자료 확인 전 학습은 보류합니다.", "원본 고객 의견 텍스트 없음 / 학습 자료 미확인"),
]


def triggers(out):
    """이번 실행의 단계 출력에서 실제로 걸린 사유만 고른다. 걸리지 않은 항목은 요청하지 않는다."""
    fin, why, extra = out.get("finance"), {}, []
    if fin:
        rc, gate = fin.get("received_claim_check"), fin.get("gate")
        if rc and rc["result"] != "성공":
            why["F3"] = f"전달받은 {rc['received']} ↔ 재계산 {rc['computed']} 불일치"
        if fin["conflict"]:
            why["F7"] = f"공식 전망(최저 {fin['official_min']:,}원)에는 하회가 없고 재전망(최저 {fin['reforecast_min']['balance']:,}원)에는 있음"
        if gate and gate["status"] in ("재무 확인 필요", "정책 확인 필요"):
            why.setdefault("F3", f"실험 #{gate['exp_id']} 집행일이 하회 구간 판단에 걸림")
            why["F11"] = f"실험 #{gate['exp_id']} 승인 경로가 '{gate['status']}'로 보류됨"
        elif fin["plans_in_window"]:
            why["F11"] = f"집행 예정일이 하회 구간에 드는 캠페인 기안 {fin['plans_in_window']}건"
    if out.get("validator") or out.get("analyst"):
        why["D8"] = "이번 실행이 표본 지표(" + ("국가별 기준선" if out.get("validator") else "실험 성과") + ")를 사용함"
    des = out.get("designer")
    if des and des.get("missing"):
        extra.append({"id": f"EXP-{des['exp_id']}", "owner": "실험 담당자", "question": f"실험 #{des['exp_id']} 입력 필요: {', '.join(des['missing'])}",
                      "source": "원본에 없음 — 실험 등록 화면에서 담당자가 직접 입력", "trigger": "설계 초안의 필수 항목 누락",
                      "status": "담당자 입력 필요", "history": [], "reply": False})
    return {"why": why, "extra": extra}


def list_requests(t, run_id=None):
    """run_id가 있으면 그 실행에서 총괄이 실제로 요청한 항목만, 없으면 전체 목록."""
    scope = t.get(f"clarifications_run:{run_id}") if run_id else None
    rows = [{"id": cid, "priority": i, "owner": owner, "question": question, "source": source,
             "as_of": t.as_of, "kind": "참가자 가정", "reason": "사용자 검토 피드백에 따른 확인 요청 설계",
             "status": "회신 접수·담당자 검토 필요" if t.get("clarification:" + cid) else "담당자 확인 필요",
             "history": t.get("clarification:" + cid, []), "reply": True,
             "note": "사용자는 담당자 자료·회신을 전달할 수 있습니다. 직접 정답을 만들 필요는 없습니다. 외부 발송 없음."}
            for i, (cid, owner, question, source) in enumerate(REQUESTS, 1)]
    if scope is None:
        return rows
    return [{**r, "trigger": scope["why"][r["id"]]} for r in rows if r["id"] in scope["why"]] + scope["extra"]


def request(t, user, run_id=None, out=None):
    """총괄이 사용자에게 정보를 요청한다(화면 표시만, 외부 발송 없음)."""
    t.require(user, "run.start")
    t.set("clarifications_requested", {"by": user, "at": now(), "as_of": t.as_of}, user)
    if run_id:
        t.set(f"clarifications_run:{run_id}", triggers(out or {}), user)
    return list_requests(t, run_id)


def reply(t, user, cid, text, source):
    t.require(user, "run.start")
    if cid not in {x[0] for x in REQUESTS}:
        raise KeyError(cid)
    if not isinstance(text, str) or not text.strip() or not isinstance(source, str) or not source.strip():
        raise ValueError("담당자 회신 내용과 자료 출처(파일·행 또는 담당자 회신 위치)가 필요합니다")
    history = t.get("clarification:" + cid, [])
    history.append({"id": f"{cid}-{len(history)+1}", "text": text.strip(), "source": source.strip(),
                    "by": user, "at": now(), "as_of": t.as_of, "kind": "합성 관측값", "mode": "모의 처리·가상 입력"})
    t.set("clarification:" + cid, history, user)
    return next(r for r in list_requests(t) if r["id"] == cid)
