"""재무팀 공식 전망(부서 주장)과 원본 스케줄 재전망(계산 결과)을 따로 계산해 나란히 둔다.
어느 쪽도 정답 처리하지 않는다. 승인 경로는 원본 approval_authority_policy 그대로 적용한다."""
import datetime as D
from collections import defaultdict

SCENARIO_KEY = "finance_scenario"
BASE_LABEL = "기준 재전망(원본 스케줄 그대로)"
SENSITIVITY = [("주말 만기 영업일 이월(A-007 대안)", {"weekend_roll": True}), ("연체 순액 기준일 반영(A-008 대안)", {"include_overdue": True})]


def _d(s):
    return D.date.fromisoformat(s)


def active_overlay(t):
    s = t.get(SCENARIO_KEY) or {}
    return s.get("overlay") or {}, s.get("label") or BASE_LABEL, s


def reforecast(t, actor="finance", via=None, overlay=None):
    """Closing[t] = Opening + 예정 회수(AR) − 예정 지급(AP 중 po_id=NONE) − 발주대금(PO). calculation_definitions §1.2.
    overlay(대안 시나리오, 기준과 분리): include_overdue, weekend_roll, ar_shift=[{channel_id, days}]"""
    overlay = overlay or {}
    R = lambda n: t.rows(actor, n, via)
    cash, ar, ap, po = R("cash_daily"), R("ar_aging"), R("ap_aging"), R("purchase_orders")
    as_of, cov = t.as_of, t.cfg["finance"]["covenant_min_krw"]
    last = max((r for r in cash if r["date"] < as_of), key=lambda r: r["date"])
    shift = {s["channel_id"]: int(s["days"]) for s in overlay.get("ar_shift", [])}
    items, overdue = [], []
    for r in ar:
        due, amt = r["due_date"], int(r["outstanding_krw"])
        it = {"date": due, "amount": amt, "type": "AR 회수예정", "ref": r["ar_id"], "channel": r["channel_id"]}
        if due < as_of:
            overdue.append(it)
            continue
        if r["channel_id"] in shift:
            it["date"] = max((_d(due) + D.timedelta(shift[r["channel_id"]])).isoformat(), as_of)
            it["type"] += f"(만기 {shift[r['channel_id']]:+d}일 시나리오)"
        items.append(it)
    po_ids, excluded = {p["po_id"] for p in po}, []
    for r in ap:
        if r["po_id"] != "NONE":
            excluded.append(r["ap_id"])  # 발주대금 재기재분 → 이중계상 금지
            continue
        it = {"date": r["payment_due_date"], "amount": -int(r["amount_krw"]), "type": "AP 지급예정(운영)", "ref": r["ap_id"]}
        (overdue if it["date"] < as_of else items).append(it)
    for r in po:
        items.append({"date": r["payment_due_date"], "amount": -int(r["amount_krw"]), "type": "발주대금", "ref": r["po_id"]})
    overdue_net = sum(i["amount"] for i in overdue)
    if overlay.get("include_overdue"):
        items += [{**i, "date": as_of, "type": i["type"] + "·연체분 기준일 반영"} for i in overdue]
    if overlay.get("weekend_roll"):
        for i in items:
            wd = _d(i["date"]).weekday()
            if wd >= 5:
                i["date"] = (_d(i["date"]) + D.timedelta(7 - wd)).isoformat()
    by = defaultdict(list)
    for i in items:
        by[i["date"]].append(i)
    bal, daily, d = int(last["closing_balance"]), [], _d(as_of)
    end = d + D.timedelta(t.cfg["finance"]["horizon_days"])
    while d <= end:
        its = by.get(d.isoformat(), [])
        bal += sum(i["amount"] for i in its)
        daily.append({"date": d.isoformat(), "weekday": d.weekday(), "inflow": sum(i["amount"] for i in its if i["amount"] > 0),
                      "outflow": -sum(i["amount"] for i in its if i["amount"] < 0), "balance": bal, "below": bal < cov, "items": its})
        d += D.timedelta(1)
    windows, cur = [], None
    for x in daily:
        if x["below"]:
            if not cur:
                cur = {"start": x["date"], "end": x["date"], "min_balance": x["balance"], "min_date": x["date"]}
                windows.append(cur)
            cur["end"] = x["date"]
            if x["balance"] < cur["min_balance"]:
                cur["min_balance"], cur["min_date"] = x["balance"], x["date"]
        else:
            cur = None
    for w in windows:
        biz = [x["date"] for x in daily if w["start"] <= x["date"] <= w["end"] and x["weekday"] < 5]
        w["business_days"] = len(biz)
        w["open_ended"] = w["end"] == daily[-1]["date"]  # 전망 지평 끝까지 하회가 이어짐
    lo = min(daily, key=lambda x: x["balance"])
    return {"kind": "계산 결과", "status": "가정 적용", "overlay": overlay, "daily": daily, "windows": windows,
            "min": {"date": lo["date"], "balance": lo["balance"]}, "covenant_min_krw": cov,
            "inputs": {"opening_date": last["date"], "opening_balance": int(last["closing_balance"]),
                       "ar_scheduled": sum(i["amount"] for i in items if i["amount"] > 0 and "AR" in i["type"]),
                       "ap_operating_scheduled": -sum(i["amount"] for i in items if "AP" in i["type"]),
                       "po_scheduled": -sum(i["amount"] for i in items if i["type"] == "발주대금"),
                       "overdue_net_excluded": 0 if overlay.get("include_overdue") else overdue_net, "overdue_net": overdue_net,
                       "ap_rows_excluded_as_po_duplicate": excluded,
                       "ap_po_amount_match": all(int(r["amount_krw"]) == next(int(p["amount_krw"]) for p in po if p["po_id"] == r["po_id"])
                                                 for r in ap if r["po_id"] in po_ids)},
            "formula": "Closing[t] = Opening[t] + AR 회수예정(outstanding_krw@due_date) − AP 지급예정(po_id=NONE, amount_krw@payment_due_date) − 발주대금(amount_krw@payment_due_date)",
            "sources": t.src("cash_daily", "ar_aging", "ap_aging", "purchase_orders") + t.cfg["finance"]["sources"],
            "assumption_ids": ["A-007", "A-008"]}


def in_window(rf, date):
    return next((w for w in rf["windows"] if w["start"] <= date <= w["end"]), None)


def compare(t, actor="finance", via=None):
    """공식 전망 vs 기준 재전망 vs (있다면) 현재 시나리오. 차이 후보 원인은 계산으로만 제시한다."""
    R = lambda n: t.rows(actor, n, via)
    off, cash, ar, chans, plans = R("cash_forecast"), R("cash_daily"), R("ar_aging"), R("channels"), R("budget_plan")
    cov = t.cfg["finance"]["covenant_min_krw"]
    base = reforecast(t, actor, via)
    overlay, label, meta = active_overlay(t)
    scen = reforecast(t, actor, via, overlay) if overlay else None
    opening = base["inputs"]["opening_balance"]
    weeks, prev, chain_ok, cum = [], opening, True, {x["date"]: x for x in base["daily"]}
    for i, r in enumerate(off):
        ws, we = r["week"], (_d(r["week"]) + D.timedelta(6)).isoformat()
        o_in, o_out, o_cl = int(r["projected_inflow_krw"]), int(r["projected_outflow_krw"]), int(r["projected_closing_krw"])
        chain_ok &= prev + o_in - o_out == o_cl
        prev = o_cl
        lo = t.as_of if i == 0 else ws  # 기준일~첫 주 시작 전 흐름은 첫 주에 포함
        days = [x for x in base["daily"] if lo <= x["date"] <= we]
        row = {"week": ws, "official_inflow": o_in, "official_outflow": o_out, "official_closing": o_cl,
               "re_inflow": sum(x["inflow"] for x in days), "re_outflow": sum(x["outflow"] for x in days),
               "re_closing": cum[we]["balance"] if we in cum else None,
               "re_min_in_week": min((x["balance"] for x in days), default=None)}
        row["closing_gap"] = None if row["re_closing"] is None else row["re_closing"] - o_cl
        weeks.append(row)
    terms = {c["channel_id"]: int(c["payment_terms_days"]) for c in chans}
    term_gap = []
    for ch in sorted({r["channel_id"] for r in ar}):
        obs = sorted({(_d(r["due_date"]) - _d(r["invoice_date"])).days for r in ar if r["channel_id"] == ch})
        if obs != [terms.get(ch)]:
            term_gap.append({"channel_id": ch, "master_terms_days": terms.get(ch), "observed_due_minus_invoice_days": obs,
                             "open_ar_krw": sum(int(r["outstanding_krw"]) for r in ar if r["channel_id"] == ch and r["due_date"] >= t.as_of)})
    worst = max(weeks, key=lambda w: w["re_outflow"] - w["official_outflow"])
    causes = [
        f"{worst['week']} 주: 재전망 유출 {worst['re_outflow']:,}원 vs 공식 전망 유출 {worst['official_outflow']:,}원. "
        f"발주대금 {base['inputs']['po_scheduled']:,}원(sc_purchase_orders_ms)이 공식 전망 유출에 그대로 들어가 있다고 보기 어렵다 — 재무팀 확인 필요",
        *[f"{g['channel_id']}: 마스터 결제조건 {g['master_terms_days']}일인데 AR 만기는 청구 후 {'·'.join(map(str, g['observed_due_minus_invoice_days']))}일. "
          f"미회수 {g['open_ar_krw']:,}원의 유입 시점이 공식 전망 가정과 다를 수 있음 — 재무팀 확인 필요" for g in term_gap],
        f"기한 경과 미수·미지급 순액 {base['inputs']['overdue_net']:,}원은 기준 재전망에 미반영(A-008). 반영 시점은 원본에 없음 — 확인 필요",
    ]
    ar_in = [r for r in ar if r["due_date"] >= t.as_of]
    late_invoice = sum(r["invoice_date"] >= t.as_of for r in ar)
    zero = [w["week"] for w in weeks if w["re_closing"] is not None and w["re_inflow"] == 0]
    recent = cash[-20:]
    o_in, o_out = [w["official_inflow"] for w in weeks], sum(w["official_outflow"] for w in weeks)
    scope = {
        "message": "재전망에는 기준일 이후 신규 매출 회수가 포함되지 않았고, 공식 전망에는 주별 유입이 반영되어 있어 전망 범위와 산정 기준이 다를 수 있습니다. 재무 담당자의 확인이 필요합니다.",
        "facts": [
            f"재전망의 유입은 fin_ar_aging_ms에 이미 잡힌 미수금 {len(ar_in)}건(합계 {base['inputs']['ar_scheduled']:,}원)의 만기 회수뿐이다. "
            + ("청구일이 모두 기준일 이전이라 기준일 이후 발생할 매출의 회수는 들어 있지 않다." if not late_invoice else f"청구일이 기준일 이후인 건이 {late_invoice}건 있다.")
            + " 원본 산식(calculation_definitions §1.2)이 정한 대로다.",
            "재전망의 유출도 운영 매입채무(fin_ap_aging_ms)와 발주대금(sc_purchase_orders_ms)의 예정분뿐이다. 기준일 이후 새로 생길 지출은 들어 있지 않다.",
            f"공식 전망(fin_cash_forecast_ms)은 {len(weeks)}주 모두에 주별 유입 {min(o_in):,}~{max(o_in):,}원(합계 {sum(o_in):,}원)과 유출(합계 {o_out:,}원)이 적혀 있다.",
            f"재전망에서 유입이 0원인 주는 {len(zero)}주다" + (f"({zero[0]} ~ {zero[-1]} 주 사이)." if zero else "."),
            f"기준일 직전 20영업일의 실제 유입은 합계 {sum(int(r['inflow_krw']) for r in recent):,}원(주 평균 약 {sum(int(r['inflow_krw']) for r in recent) // 4:,}원), "
            f"유출은 {sum(int(r['outflow_krw']) for r in recent):,}원(주 평균 약 {sum(int(r['outflow_krw']) for r in recent) // 4:,}원)이다(fin_cash_daily_ms). "
            f"공식 전망의 주별 유출은 {min(w['official_outflow'] for w in weeks):,}~{max(w['official_outflow'] for w in weeks):,}원이다."],
        "candidates": [
            "공식 전망의 주별 유입에 기준일 이후 신규 매출 회수가 들어 있을 수 있다. 공식 전망의 구성 내역은 원본에 없어 확인되지 않았다.",
            "원본 산식이 예정 스케줄만 합산하도록 정한 것이 의도된 보수적 기준인지, 신규 유입·유출을 따로 더해야 하는지는 원본에 없다.",
            "두 전망은 포함 범위가 달라 최저 잔액을 그대로 비교하기 어려울 수 있다. 어느 쪽이 하회 판단의 기준인지는 재무 담당이 정해야 한다."],
        "kind": "사실 = 원본과 계산 코드를 대조한 결과 / 후보 = 미확인(재무 담당 확인 필요)"}
    causes.insert(0, "전망 범위 차이: " + scope["message"])
    w0 = base["windows"][0] if base["windows"] else None
    claim = next(iter(R("received_window")), None)  # 전달받은 예비 하회 구간(있을 때만 대조)
    plan_rows = []
    for p in plans:
        w = in_window(scen or base, p["settlement_date"])
        plan_rows.append({"plan_id": p["plan_id"], "campaign_id": p["campaign_id"], "requested_budget_krw": int(p["requested_budget_krw"]),
                          "settlement_date": p["settlement_date"], "contract_deadline": p["contract_deadline"], "in_window": bool(w),
                          "over_single_limit": int(p["requested_budget_krw"]) > t.cfg["approval_rules"]["single_limit_krw"],
                          "deadline_before_window": bool(w) and p["contract_deadline"] < w["start"]})
    hist_min = min(cash, key=lambda r: int(r["closing_balance"]))
    return {
        "covenant_min_krw": cov, "as_of": t.as_of,
        "official": {"kind": "부서 주장·분석", "status": "사용자 확인 필요", "source": t.src("cash_forecast")[0], "forecast_date": off[0]["forecast_date"],
                     "min_closing": min(int(r["projected_closing_krw"]) for r in off), "below_covenant_weeks": [r["week"] for r in off if int(r["projected_closing_krw"]) < cov],
                     "chain_identity_ok": chain_ok, "granularity": "주 단위(일별 최저 잔액은 알 수 없음)",
                     "unknown": ["공식 전망의 입력값·산정식·가정은 원본에 없음 → 재무팀 확인 필요",
                                 "발주대금·AR 회수 조건(결제조건이 바뀐 거래처 포함)·연체 순액의 반영 여부와 유입·유출 시점 미확인 — 재무 담당 확인 필요"]},
        "reforecast": {k: base[k] for k in ("kind", "status", "windows", "min", "inputs", "formula", "sources", "assumption_ids")},
        "scenario": None if not scen else {"label": label, "meta": meta, "windows": scen["windows"], "min": scen["min"], "overlay": overlay,
                                           "kind": "계산 결과", "status": "가정 적용(대안 시나리오 — 기준과 분리)"},
        "conflict": bool(base["windows"]) and not [r for r in off if int(r["projected_closing_krw"]) < cov],
        "weeks": weeks, "candidate_causes": causes, "scope_gap": scope, "term_gap": term_gap,
        "series": {"base": [[x["date"], x["balance"]] for x in base["daily"]],
                   "scenario": [[x["date"], x["balance"]] for x in scen["daily"]] if scen else None,
                   "official": [[(_d(r["week"]) + D.timedelta(6)).isoformat(), int(r["projected_closing_krw"])] for r in off]},
        "actual": {"identity_fail_rows": sum(int(r["opening_balance"]) + int(r["inflow_krw"]) - int(r["outflow_krw"]) != int(r["closing_balance"]) for r in cash),
                   "rows": len(cash), "min_closing": int(hist_min["closing_balance"]), "min_date": hist_min["date"],
                   "breach_days": sum(int(r["closing_balance"]) < cov for r in cash)},
        "budget_plans": {"rows": plan_rows, "total_requested_krw": sum(r["requested_budget_krw"] for r in plan_rows),
                         "in_window_count": sum(r["in_window"] for r in plan_rows),
                         "note": "기안 금액은 대표 표본 계열이라 현금 전망에 합산하지 않는다. 여기서는 집행 예정일·확정기한이 하회 구간과 겹치는지만 대조한다(G3-5)."},
        "received_claim_check": None if not (w0 and claim) else {
            "received": f"하회 구간 {claim['start']} ~ {claim['end']} (전달받은 예비 정보)", "computed": f"{w0['start']} ~ {w0['end']}",
            "result": "성공" if (w0["start"], w0["end"]) == (claim["start"], claim["end"]) else "실패",
            "note": f"가정 기반 재계산의 최저점은 {w0['min_date']} {w0['min_balance']:,}원, 하회 구간은 {w0['end']}까지다. 확정 전망이 아니다. 최저점 날짜를 종료일로 사용하지 말고 재무 담당에게 유입·유출 시점을 확인한다."},
        "policy_findings": {
            "confirmed_in_source": ["covenant 하회 예상 구간 중 신규 현금 약정은 금액·종류 불문 그룹 CEO 승인(approval_authority_policy.md, covenant_terms_ms.md)",
                                    "구간 특정은 재전망(AR·AP·발주 스케줄 합산) 기준", "대조 대상은 약정일이 아니라 현금 유출일",
                                    "확정기한이 하회 구간보다 먼저 오면 기한 전에 상신"],
            "unconfirmed": t.cfg["approval_rules"]["unconfirmed"]},
    }


def review(t, exp, actor="finance", via=None):
    """실험 1건의 승인 경로 판정. 자동 승인은 하지 않는다 — 누구에게 올릴지만 정한다."""
    t.require(actor, "finance.review", f"experiment:{exp['id']}", via)
    d, rules = exp["data"], t.cfg["approval_rules"]
    overlay, label, _ = active_overlay(t)
    rf = reforecast(t, actor, via, overlay)
    budget, out_date = d.get("budget_krw"), d.get("cash_out_date")
    g = {"evaluated_at_as_of": t.as_of, "scenario": label, "windows": rf["windows"], "reasons": [], "approver": None, "group_report": False,
         "sources": ["00_common/approval_authority_policy.md", "02_masil/financials/covenant_terms_ms.md"], "assumption_ids": ["A-009"]}
    if not budget or not out_date:
        g.update(status="정보 부족", reasons=["예산 또는 현금 집행 예정일이 없어 재무 검토를 할 수 없음"])
        return g
    w = in_window(rf, out_date)
    g["in_window"] = bool(w)
    if w:
        g["reasons"].append(f"현금 집행 예정일 {out_date}이 재전망 하회 예상 구간({w['start']} ~ {w['end']}, 최저 {w['min_balance']:,}원)에 들어감")
        if d.get("decision_deadline") and d["decision_deadline"] < w["start"]:
            g["reasons"].append(f"확정기한 {d['decision_deadline']}이 하회 구간보다 먼저 도래 → 기한 전에 상신해야 함(covenant_terms_ms)")
        if exp.get("finance_confirmed_scenario") == label:
            g.update(status="정책 확인 필요", approver=None)
            g["reasons"].append("그룹 상신 대상(CEO/이사회). 정책 담당자에게 승인 주체·순서·필요 증빙을 확인해야 하며 재무 회신만으로 최종 승인할 수 없음")
        else:
            g.update(status="재무 확인 필요", approver=None)
            g["reasons"].append("공식 전망과 재전망이 충돌하므로 재무 담당의 구간 확인이 먼저 필요 — 상위 승인 가능성 검토 필요")
        return g
    g["reasons"].append(f"현금 집행 예정일 {out_date}은 하회 예상 구간 밖({label})")
    # F3: 구간 끝은 가정(A-007 주말 이월, A-008 연체분)에 따라 달라진다. 가정을 바꾸면 구간에 드는 집행일은 재무 확인 없이 평시 전결로 보내지 않는다.
    edge = [name for name, ov in SENSITIVITY if not overlay.get(next(iter(ov))) and in_window(reforecast(t, actor, via, {**overlay, **ov}), out_date)]
    if edge:
        g["sensitivity"] = edge
        if exp.get("finance_confirmed_scenario") != label:
            g.update(status="재무 확인 필요", approver=None)
            g["reasons"].append(f"다만 {' / '.join(edge)} 가정을 적용하면 하회 구간에 들어감 — 구간 종료일은 확정 전망이 아니므로 재무 담당의 유입·유출 시점 확인이 먼저 필요")
            return g
        g["reasons"].append(f"{' / '.join(edge)} 가정 적용 시 하회 구간에 드는 날짜이나 재무 담당이 현재 기준({label})을 확인함(가상 입력) — 평시 승인 경로 적용")
    total = d.get("proposal_total_krw") or budget
    if budget <= rules["single_limit_krw"] and total <= rules["proposal_total_limit_krw"]:
        g.update(status="승인 대기", approver=rules["within_limit_approver"])
        g["reasons"].append(f"건별 {budget:,}원 ≤ {rules['single_limit_krw']:,}원 → 마케팅팀 전결")
    else:
        g.update(status="승인 대기", approver=rules["over_limit_approver"], group_report=rules["over_limit_group_report"])
        g["reasons"].append(f"건별 {budget:,}원 또는 기안 총액 {total:,}원이 한도 초과 → 대표 승인 + 그룹 보고")
    return g
