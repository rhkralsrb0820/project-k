"""고객사 문서 검색(RAG의 검색부). 표준 라이브러리 BM25 + 한글 2글자 토큰.
검색 범위 = 해당 고객사 설정의 documents뿐. 근거가 약하면 결과를 만들지 않고 '근거 부족'을 돌려준다.
ponytail: 임베딩 없이 어휘 일치만 본다. 동의어·의미 검색이 필요해지면 임베딩(+CUDA 경로)으로 교체."""
import math, re
from collections import Counter
from pathlib import Path

MIN_COVERAGE = 0.5  # 질의 토큰의 절반 이상이 한 단락에 있어야 근거로 인정


def _tok(s):
    out = []
    for w in re.findall(r"[0-9a-z_]+|[가-힣]+", s.lower()):
        out += [w] if w[0] < "가" or len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)]
    return out


def _index(t):
    if "rag" in t._cache:
        return t._cache["rag"]
    chunks = []
    for doc in t.cfg["documents"]:
        lines = (t.data_root / doc["path"]).read_text(encoding="utf-8-sig").splitlines()
        head, cur = "", None
        for n, line in enumerate(lines, 1):
            if not line.strip():
                cur = None
                continue
            if line.startswith("#"):
                head, cur = line.lstrip("# ").strip(), None
            if cur is None or line.startswith("- "):
                cur = {"doc": Path(doc["path"]).name, "path": doc["path"], "class": doc["class"], "heading": head,
                       "line_start": n, "line_end": n, "text": line}
                chunks.append(cur)
            else:
                cur["text"] += "\n" + line
                cur["line_end"] = n
    for c in chunks:
        c["tf"] = Counter(_tok(c["heading"] + " " + c["text"]))
        c["len"] = sum(c["tf"].values())
    df = Counter(k for c in chunks for k in c["tf"])
    t._cache["rag"] = (chunks, df, sum(c["len"] for c in chunks) / max(len(chunks), 1))
    return t._cache["rag"]


def search(t, actor, query, via=None, k=5):
    t.require(actor, "rag.search", query[:60], via)
    chunks, df, avg = _index(t)
    allowed = set(t.policy()["roles"].get(t.role_of(actor), {}).get("doc_classes", []))
    q = set(_tok(query))
    scored, hidden = [], 0
    for c in chunks:
        hit = [w for w in q if w in c["tf"]]
        if not hit:
            continue
        if c["class"] not in allowed:
            hidden += 1
            continue
        s = sum(math.log(1 + (len(chunks) - df[w] + 0.5) / (df[w] + 0.5)) * c["tf"][w] * 2.5 / (c["tf"][w] + 1.5 * (0.25 + 0.75 * c["len"] / avg)) for w in hit)
        scored.append((s, len(hit) / len(q), c))
    scored.sort(key=lambda x: -x[0])
    good = [x for x in scored if x[1] >= MIN_COVERAGE][:k]
    hits = [{"doc": c["doc"], "path": c["path"], "class": c["class"], "heading": c["heading"], "line_start": c["line_start"],
             "line_end": c["line_end"], "text": c["text"], "score": round(s, 2), "coverage": round(cov, 2)} for s, cov, c in good]
    ql = query.lower()  # 충돌 등록부는 상위 k건에 해당 문서가 들었는지와 무관하게 질의 주제로 대조한다
    conflicts = [c for c in t.cfg.get("known_conflicts", []) if any(kw.lower() in ql for kw in c["keywords"])]
    return {"query": query, "scope": f"{t.id} 등록 문서 {len(t.cfg['documents'])}건", "hits": hits, "insufficient": not hits,
            "message": "근거 부족: 등록 문서에서 질의와 충분히 일치하는 단락을 찾지 못함. 답을 만들어내지 않는다." if not hits else None,
            "hidden_by_permission": hidden, "conflicts": conflicts,
            "conflict_note": "충돌 표시는 설정에 수동 등록된 항목(known_conflicts)만 대상으로 한다 — 자동 탐지 아님"}
