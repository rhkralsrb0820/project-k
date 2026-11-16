"""LLM 연결 지점. **현재 연결된 모델은 없다.** 등록된 연결(PROVIDERS)이 비어 있으므로 모든 요청은 '수행 안 함'을 돌려주고
기존 규칙 기반 처리가 그대로 진행된다. 이 파일은 외부로 아무것도 보내지 않는다.

나중에 연결할 때:
1. PROVIDERS에 {"kind": "external" 또는 "local", "call": fn(model, prompt) -> str} 를 등록한다.
2. 설정 파일의 "llm"에 provider·model을 적는다. API 키는 파일에 넣지 않고 환경변수 이름(api_key_env)만 적는다.
3. 외부 연결은 관리자가 정책에서 external_ai_transfer를 켜고 external_ai.send를 허용해야 한다. 로컬 연결은 llm.local을 허용해야 한다.

LLM이 하는 일은 제안뿐이다: 요청 문장 해석(작업 종류 제안), 보고서 문장 초안. 계산·재무 판정·승인·상태 변경은 하지 않으며
그 기능을 호출할 수단도 받지 않는다. 실행되는 작업은 화면에서 고른 작업 유형이다."""
import json
from .core import ALLOW, now

PROVIDERS = {}  # 구현된 연결만 등록한다. 지금은 비어 있다.
ACTION = {"external": "external_ai.send", "local": "llm.local"}
RULE_LABEL = "규칙 기반 / LLM 미연결"
PLANNED = ["사용자 요청 해석(작업 유형 제안)", "에이전트 작업 계획 초안", "보고서·기획서 문장 초안", "자료 검색 결과 해석과 출처 표시"]


def status(t):
    cfg = t.cfg.get("llm") or {}
    name = cfg.get("provider") or "none"
    p = PROVIDERS.get(name)
    reason = None if p else ("LLM 설정 없음(provider=none)" if name == "none" else f"설정된 연결 '{name}'의 구현이 등록되지 않음")
    return {"provider": name, "model": cfg.get("model") if p else None, "kind": p["kind"] if p else None, "connected": bool(p),
            "mode": "규칙 기반", "label": RULE_LABEL if not p else f"규칙 기반 처리 · 모델 연결됨({cfg.get('model')})",
            "llm_state": "연결됨" if p else "연결되지 않음", "free_text": "제안만 받음(실행은 선택한 작업 유형)" if p else "미지원",
            "connect": "설정됨" if p else "준비 중(연결 구현·설정·정책 허용 필요)", "reason": reason, "planned_roles": PLANNED}


def _ask(t, purpose, prompt):
    """반환 (모델 응답 또는 None, 수행하지 않은 사유). 정책 검사를 통과해야만 모델을 부른다."""
    st = status(t)
    if not st["connected"]:
        return None, st["reason"]
    p = PROVIDERS[st["provider"]]
    res = f"{purpose}:{len(prompt)}자"  # 감사 로그에는 내용이 아니라 용도와 길이만 남긴다
    if p["kind"] == "external" and not t.policy().get("external_ai_transfer"):
        t.q("INSERT INTO audit(ts,actor,via,action,resource,decision,reason) VALUES(?,?,?,?,?,?,?)",
            (now(), "orchestrator", None, ACTION["external"], res, "금지", "외부 AI 전송 정책(external_ai_transfer)이 꺼져 있음"))
        return None, "외부 AI 전송 정책이 꺼져 있어 보내지 않음"
    d = t.check("orchestrator", ACTION[p["kind"]], res)
    if d["decision"] != ALLOW:
        return None, f"정책 검사 {d['decision']}: {d['reason']}"
    try:
        return str(p["call"](st["model"], prompt)), None
    except Exception as e:  # 모델 장애가 업무 처리를 멈추지 않게 한다
        return None, f"모델 호출 실패({type(e).__name__}) — 규칙 기반으로 처리"


def interpret(t, goal, workflows):
    """요청 문장 해석. 결과는 제안일 뿐이며 실행되는 작업을 바꾸지 않는다. 보내는 내용은 목표 문장과 작업 이름뿐이다."""
    if not goal:
        return {"performed": False, "reason": "목표 문장 없음", "suggested_workflow": None}
    out, why = _ask(t, "요청 해석", "다음 요청에 맞는 작업 하나를 JSON {\"workflow\": 이름}으로 답하라.\n작업: " + ", ".join(workflows) + "\n요청: " + goal)
    if out is None:
        return {"performed": False, "reason": why, "suggested_workflow": None}
    try:
        wf = json.loads(out).get("workflow")
    except Exception:
        wf = None
    if wf not in workflows:  # 정해진 작업 밖의 답(예: 승인·집행)은 버린다
        return {"performed": True, "reason": "모델 응답이 허용된 작업 목록 밖이라 버림", "suggested_workflow": None}
    return {"performed": True, "reason": None, "suggested_workflow": wf, "model": status(t)["model"]}


def draft(t, facts):
    """보고서 요약 문장 초안. facts = 이미 계산·검증된 요약 줄(원본 행은 보내지 않는다). 반환 (문장 또는 None, 사유, 모델명)."""
    out, why = _ask(t, "보고서 문장", "아래 사실만으로 3문장 이내 요약을 쓰라. 새 수치·판단을 만들지 말 것.\n" + "\n".join(facts))
    return out, why, (status(t)["model"] if out else None)


def authoring(model=None):
    """보고서에 적는 작성 방식. 모델이 실제로 문장을 만든 경우에만 모델을 적는다."""
    return f"규칙 기반 처리 + 연결된 모델의 문장 생성({model})" if model else RULE_LABEL
