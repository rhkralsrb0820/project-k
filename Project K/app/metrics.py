"""데이터 연결 검증과 국가별 기준선. 모든 값에 종류(G3-3)·상태·정의·출처·가정을 붙인다."""
import datetime as D
from collections import defaultdict

SAMPLE_NOTE = ("마케팅 주문·반품·광고비는 대표 표본이다. 표본 내 지표는 계산 가능하지만 전체 시장·전사 성과로 일반화할 수 없다. "
               "데이터 제공자에게 추출 모집단·기간·방식·누락 기준 확인 필요. 전사 매출·현금·예산과 합산하거나 금액 효과로 환산하지 않는다.")
LABELS = {"orders": "주문 수", "return_rate": "반품률", "nominal_roas": "명목 ROAS",
          "real_roas": "실질 ROAS", "repurchase_90d": "90일 재구매율"}


def linkage(t, actor="validator", via=None):
    R = lambda n: t.rows(actor, n, via)
    orders, rets, camps, costs = R("orders"), R("returns"), R("campaigns"), R("cost_master")
    ads, rep, chans = R("ad_spend"), R("repurchase"), R("channels")
    cmap = {c["campaign_id"]: c for c in camps}
    kmap = {(c["sku_id"], c["channel_id"]) for c in costs}
    omap = {o["order_id"]: o for o in orders}
    chmap = {c["channel_id"]: c for c in chans}
    cust_c = defaultdict(set)
    for o in orders:
        cust_c[o["customer_ref"]].add(o["country"])
    checks = []

    def add(cid, name, bad, total, src):
        checks.append({"id": cid, "name": name, "total": total, "unmatched": bad,
                       "result": "성공" if bad == 0 else "실패", "sources": t.src(*src)})

    add("L1", "주문 → 캠페인 마스터(campaign_id)", sum(o["campaign_id"] not in cmap for o in orders), len(orders), ["orders", "campaigns"])
    add("L2", "주문 → 원가 마스터(sku_id+channel_id)", sum((o["sku_id"], o["channel_id"]) not in kmap for o in orders), len(orders), ["orders", "cost_master"])
    add("L3", "반품 → 주문(order_id)", sum(r["order_id"] not in omap for r in rets), len(rets), ["returns", "orders"])
    add("L4", "주문 국가 = 캠페인 국가", sum(o["campaign_id"] in cmap and o["country"] != cmap[o["campaign_id"]]["country"] for o in orders), len(orders), ["orders", "campaigns"])
    add("L5", "주문 국가 = 판매채널 국가", sum(o["channel_id"] not in chmap or o["country"] != chmap[o["channel_id"]]["country"] for o in orders), len(orders), ["orders", "channels"])
    add("L6", "광고비 → 캠페인 마스터", sum(a["campaign_id"] not in cmap for a in ads), len(ads), ["ad_spend", "campaigns"])
    add("L7", "재구매 고객 → 주문 고객(customer_ref)", sum(r["customer_ref"] not in cust_c for r in rep), len(rep), ["repurchase", "orders"])
    add("L8", "주문 ID 중복 없음", len(orders) - len(omap), len(orders), ["orders"])
    add("L9", "환불액 ≤ 원 주문 GMV", sum(r["order_id"] in omap and int(r["refund_amount_krw"]) > int(omap[r["order_id"]]["gmv_krw"]) for r in rets), len(rets), ["returns", "orders"])
    add("L10", "고객당 주문 국가 1개(A-005 근거)", sum(len(v) > 1 for v in cust_c.values()), len(cust_c), ["orders"])
    add("L11", "인플루언서 피 = 광고비(이미 포함, 재가산 금지)", sum(a["influencer_fee_krw"] != "NONE" and a["influencer_fee_krw"] != a["ad_cost_krw"] for a in ads), sum(a["influencer_fee_krw"] != "NONE" for a in ads), ["ad_spend"])
    return {"checks": checks, "failed": [c["id"] for c in checks if c["result"] != "성공"],
            "period": {"주문일": [min(o["order_date"] for o in orders), max(o["order_date"] for o in orders)],
                       "환불완료일": [min(r["refund_completed_date"] for r in rets), max(r["refund_completed_date"] for r in rets)],
                       "광고 집행일": [min(a["spend_date"] for a in ads), max(a["spend_date"] for a in ads)]},
            "unverifiable": ["표본 추출 방식과 국가·채널별 대표성: 원본에 근거 없음 → 확인 필요",
                             "last-click 7일 귀속의 주문 단위 검증: 주문 행에 클릭 시점이 없어 재현 불가"]}


def baselines(t, actor="validator", via=None):
    R = lambda n: t.rows(actor, n, via)
    orders, rets, camps, costs, ads, rep = R("orders"), R("returns"), R("campaigns"), R("cost_master"), R("ad_spend"), R("repurchase")
    assume = {a["assumption_id"]: a for a in R("assumptions")}
    cmap = {c["campaign_id"]: c for c in camps}
    kmap = {(c["sku_id"], c["channel_id"]): c for c in costs}
    refund = defaultdict(int)
    for r in rets:
        refund[r["order_id"]] += int(r["refund_amount_krw"])
    a = defaultdict(lambda: defaultdict(float))
    cust_country = {}
    for o in orders:
        c, g, k = o["country"], int(o["gmv_krw"]), kmap[(o["sku_id"], o["channel_id"])]
        rf, x = refund.get(o["order_id"], 0), a[o["country"]]
        cust_country[o["customer_ref"]] = c
        x["n"] += 1; x["gmv"] += g; x["refund"] += rf
        x["cogs"] += int(k["product_cost_krw"]); x["ship"] += int(k["shipping_cost_krw"])
        x["fee_gmv"] += g * float(k["channel_fee_pct"]) / 100
        x["fee_net"] += (g - rf) * float(k["channel_fee_pct"]) / 100
        if rf:
            x["retlog"] += int(k["return_logistics_cost_krw"])
    for s in ads:
        a[cmap[s["campaign_id"]]["country"]]["ad"] += int(s["ad_cost_krw"])
    as_of = D.date.fromisoformat(t.as_of)
    for r in rep:
        x = a[cust_country[r["customer_ref"]]]
        if D.date.fromisoformat(r["first_order_date"]) + D.timedelta(90) <= as_of:
            x["mature"] += 1; x["mature_re"] += r["reorder_within_90d"] == "True"
        else:
            x["immature"] += 1

    period = f"주문일 {min(o['order_date'] for o in orders)} ~ {max(o['order_date'] for o in orders)} (기준일 {t.as_of})"

    def m(value, status, definition, population, unit, attribution, src, assumptions=(), note="", kind="계산 결과"):
        return {"value": value, "kind": kind, "status": status, "definition": definition, "population": population,
                "period": period, "unit": unit, "attribution": attribution, "sources": t.src(*src),
                "assumptions": [assume[i] for i in assumptions], "note": note}

    out = {}
    for c in t.cfg["countries"]:
        x = a.get(c)
        if not x or not x["n"]:
            out[c] = {k: {"value": None, "kind": "계산 결과", "status": "데이터 없음", "note": "해당 국가 주문 없음"} for k in LABELS}
            continue
        net = x["gmv"] - x["refund"]
        cp = net - x["cogs"] - x["ad"] - x["fee_gmv"] - x["ship"] - x["retlog"]
        cp_alt = net - x["cogs"] - x["ad"] - x["fee_net"] - x["ship"] - x["retlog"]
        ad = x["ad"]
        out[c] = {
            "orders": m(int(x["n"]), "원본에서 확인됨", "주문 행 수", f"{c} 주문 전체(표본)", "건", "mkt_orders_ms.country", ["orders"]),
            "return_rate": m(x["refund"] / x["gmv"] if x["gmv"] else None, "원본 기반 계산" if x["gmv"] else "데이터 없음",
                             "Σ 반품환불액 / Σ GMV (금액 기준, 건수 아님)", f"{c} 주문 전체와 기준일까지 승인된 환불(표본)", "비율",
                             "환불을 원 주문(order_id)에 연결. 전 기간 합계라 승인일 기준 집계와 같은 값",
                             ["returns", "orders"], note="최근 주문은 아직 환불이 접수되지 않았을 수 있어 과소일 수 있음"),
            "nominal_roas": m(x["gmv"] / ad if ad else None, "원본 기반 계산" if ad else "데이터 없음",
                              "GMV(쿠폰 반영 후) / Marketing Spend(ad_cost_krw 전액: 인플루언서+퍼포먼스+오프라인)",
                              f"{c} 캠페인 귀속 주문 전체(표본)", "배", "캠페인 국가 기준, last-click 7일(원본 귀속값 사용)",
                              ["orders", "ad_spend", "campaigns"], note="1을 넘어도 흑자를 뜻하지 않는다(원가·수수료·물류 차감 전)" if ad else "광고비 0 → 계산 불가"),
            "real_roas": m(cp / ad if ad else None, "가정 적용" if ad else "데이터 없음",
                           "공헌이익 / Marketing Spend. 공헌이익 = Net Revenue − 제품원가 − 마케팅비 − 채널수수료 − 출고·반품 물류비(각 1회, 쿠폰·환불 재차감 없음)",
                           f"{c} 캠페인 귀속 주문 전체(표본)", "배", "캠페인 국가 기준, last-click 7일(원본 귀속값 사용)",
                           ["orders", "returns", "cost_master", "ad_spend", "campaigns"], ["A-001", "A-002", "A-003", "A-004"],
                           note=f"예비 결과: 비용 가정에 따라 달라진다. 대안 가정(수수료를 환불 후 순매출에 적용) 시 {cp_alt / ad:.3f}" if ad else "광고비 0 → 계산 불가"),
            "repurchase_90d": m(x["mature_re"] / x["mature"] if x["mature"] else None, "가정 적용" if x["mature"] else "데이터 없음",
                                "90일 내 재구매 고객수 / 최초구매 고객수 (reorder_within_90d = True)",
                                f"{c} 최초구매 고객 중 90일 관찰이 끝난 {int(x['mature']):,}명 (관찰 미완료 {int(x['immature']):,}명 제외)",
                                "비율", "고객 국가 = 주문 국가", ["repurchase", "orders"], ["A-005", "A-006"],
                                note="관찰 미완료 고객은 비재구매로 단정하지 않고 판정에서 제외"),
        }

    prelim = []
    for p in R("preliminary"):
        cell = out.get(p["country"], {}).get(p["metric"], {})
        got, v = float(p["received_value"]), cell.get("value")
        digits = 0 if p["metric"] == "orders" else 3 if p["metric"] in ("return_rate", "repurchase_90d") else 2
        prelim.append({"id": p["prelim_id"], "country": p["country"], "metric": p["metric"], "received": got, "computed": v,
                       "result": "데이터 부족" if v is None else "성공" if round(v, digits) == round(got, digits) else "실패"})
    return {"as_of": t.as_of, "sample_note": SAMPLE_NOTE, "labels": LABELS, "countries": out, "preliminary": prelim,
            "assumptions": assume}
