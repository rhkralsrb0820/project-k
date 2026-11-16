"""업무함 보드의 「확인 처리」와 「완료함」.
확인 처리 = 시스템 관리자가 카드를 보드에서 내리는 것. 기록은 남고(삭제 아님) 완료함에서 다시 볼 수 있으며 복구할 수 있다.
업무 자체는 바꾸지 않는다 — 실험 상태, 승인, 열린 작업은 그대로다. 그래서 홈의 「처리할 일」에는 열린 작업이 계속 보인다."""
from .core import now


def archive(t, user, key, category, title, meta, note, exp_id=None):
    t.require(user, "board.archive", key)
    if not isinstance(key, str) or not key or not title:
        raise ValueError("확인 처리할 항목이 지정되지 않음")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("확인 내용을 입력해야 함(완료함 기록에 남는다)")
    if t.q("SELECT 1 FROM board_archive WHERE item_key=? AND active=1", (key,)):
        raise ValueError("이미 확인 처리된 항목")
    aid = t.q("INSERT INTO board_archive(item_key,category,title,meta,note,exp_id,by_user,archived_at,active) VALUES(?,?,?,?,?,?,?,?,1)",
              (key, category, str(title)[:500], str(meta or "")[:300], note.strip(), exp_id, user, now()))
    if exp_id:  # 실험과 이어진 카드는 실험 이력에도 남긴다
        t.q("INSERT INTO history(exp_id,ts,actor,event,detail) VALUES(?,?,?,?,?)", (exp_id, now(), user, "관리자 확인 처리(보드에서 내림)", f"{category}: {note.strip()} · 실험 상태·승인은 바뀌지 않음"))
    return listing(t)


def restore(t, user, aid):
    t.require(user, "board.archive", f"archive:{aid}")
    if not t.q("SELECT 1 FROM board_archive WHERE id=? AND active=1", (aid,)):
        raise KeyError(f"완료함 기록 #{aid} 없음(또는 이미 복구됨)")
    t.q("UPDATE board_archive SET active=0, restored_by=?, restored_at=? WHERE id=?", (user, now(), aid))
    return listing(t)


def listing(t):
    return {"items": t.q("SELECT * FROM board_archive WHERE active=1 ORDER BY id DESC"),
            "closed_todos": t.q("SELECT * FROM todos WHERE status='완료' ORDER BY id DESC LIMIT 200"),
            "restored": t.q("SELECT COUNT(*) n FROM board_archive WHERE active=0")[0]["n"]}
