"""대상 상품(SKU) 공급 여력 확인 — 필수 확장 + 다른 부서(SCM·재고) 제약 반영.

원본 기록: sku_inventory_ms(현재고·운송 중·재고 커버), m_sku_ms(안전재고 하한·리드타임), sku_weekly_sales_ms(주간 판매).
계산 결과: 재고 커버(주) = on_hand_qty ÷ 최근 4주 평균 주간판매량(calculation_definitions 1.3, 운송 중 미합산).
참가자 가정 A-014: 커버가 원본 안전재고 하한 미만인 상품의 실험은 SCM 공급 확인 회신 전에는 상신하지 않는다.
SCM 회신은 가상 입력이고 승인이 아니다. 실험 수요량은 표본에서 환산하지 않는다(G3-5) — 수량 효과는 판단 유보.
"""
import datetime
from .core import now

KEY = "supply_reply:"
ACTOR = "orchestrator"


def table(t):
    """SKU별 공급 여력. 원본은 실행 중 바뀌지 않으므로 한 번만 계산한다."""
    if "supply_table" not in t._cache:
        inv = {r["sku_id"]: r for r in t.rows(ACTOR, "inventory")}
        sales = {}
        for r in t.rows(ACTOR, "weekly_sales"):
            sales.setdefault(r["sku_id"], []).append((r["week"], float(r["sold_qty"])))
        out = {}
        for s in t.rows(ACTOR, "skus"):
            i = inv.get(s["sku_id"])
            last4 = [q for w, q in sorted(sales.get(s["sku_id"], [])) if not i or w <= i["snapshot_date"]][-4:]
            avg = sum(last4) / len(last4) if last4 else None
            cover = float(i["on_hand_qty"]) / avg if i and avg else None  # 분모 0·자료 없음은 계산 불가
            smin = float(s["safety_stock_weeks_min"])
            out[s["sku_id"]] = {
                "sku_id": s["sku_id"], "name": s["sku_name"], "category": s["category"], "source_company": s["source_company"],
                "cover_weeks": None if cover is None else round(cover, 2), "cover_recorded": float(i["stock_cover_weeks"]) if i else None,
                "safety_min_weeks": smin, "lead_time_weeks": int(s["lead_time_weeks"]), "on_hand_qty": int(i["on_hand_qty"]) if i else None,
                "in_transit_qty": int(i["in_transit_qty"]) if i else None, "snapshot_date": i["snapshot_date"] if i else None,
                "short": None if cover is None else cover < smin, "kind": "계산 결과(원본 기반)",
                "sources": t.src("inventory", "weekly_sales", "skus")}
        t._cache["supply_table"] = out
    return t._cache["supply_table"]


def get_reply(t, sku_id):
    return t.get(KEY + sku_id)


def set_reply(t, user, sku_id, available, note, exp_id):
    t.set(KEY + sku_id, {"available": available, "note": note, "by": user, "at": now(), "as_of": t.as_of, "exp_id": exp_id,
                         "kind": "가상 입력(SCM 모의 회신) — 실제 확보한 증거가 아님"}, user)


def check(t, d):
    """실험 설계 d의 대상 상품 공급 판정. 상품 미입력·마스터에 없음이면 None."""
    s = table(t).get(d.get("sku_id") or "")
    if not s:
        return None
    r = dict(s, reply=get_reply(t, s["sku_id"]), reasons=[], alternatives=[])
    if s["short"] is None:
        r.update(state="공급 확인 필요", message=f"공급: {s['sku_id']} 재고 커버 계산 불가(재고 또는 판매 자료 없음) — SCM 확인 필요")
        return r
    r["reasons"].append(f"{s['sku_id']} {s['name']} 재고 커버 {s['cover_weeks']}주(현재고 {s['on_hand_qty']:,}EA ÷ 최근 4주 평균 판매, "
                        f"{s['snapshot_date']} 기준) · 안전재고 하한 {s['safety_min_weeks']:g}주(m_sku_ms)")
    if not s["short"]:
        r.update(state="충족", message=None)
        return r
    r["alternatives"] = [{k: x[k] for k in ("sku_id", "name", "cover_weeks")} for x in table(t).values()
                         if x["category"] == s["category"] and x["short"] is False]
    if d.get("start_date"):
        weeks = (datetime.date.fromisoformat(d["start_date"]) - datetime.date.fromisoformat(t.as_of)).days / 7
        if s["lead_time_weeks"] > weeks:
            r["reasons"].append(f"발주 리드타임 {s['lead_time_weeks']}주 > 실험 시작({d['start_date']})까지 {weeks:.1f}주 — 신규 발주로는 시작 전 보충 불가. "
                                f"운송 중 {s['in_transit_qty']:,}EA의 입고일은 원본에 없음(미확인)")
    alt = ", ".join(f"{a['sku_id']} {a['name']}({a['cover_weeks']}주)" for a in r["alternatives"]) or "같은 제품군에 하한 이상 상품 없음 — 일정 연기 검토"
    rep = r["reply"]
    if not rep:
        r.update(state="공급 확인 필요", message=f"공급: {s['sku_id']} 재고 커버 {s['cover_weeks']}주 < 안전재고 하한 {s['safety_min_weeks']:g}주 — SCM 공급 확인 회신 필요")
    elif rep["available"]:
        r.update(state="공급 가능(회신)", message=None)
        r["reasons"].append(f"SCM 회신(가상 입력, {rep['at'][:10]}): 공급 가능 — {rep['note']}")
    else:
        r.update(state="공급 불가", message=f"공급: {s['sku_id']} SCM 회신 '공급 불가'({rep['note']}) — 대상 상품 또는 일정 변경 필요. 대안: {alt}")
        r["reasons"].append(f"SCM 회신(가상 입력, {rep['at'][:10]}): 공급 불가 — {rep['note']}")
    r["alternatives_text"] = alt
    return r
