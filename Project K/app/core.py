"""Project K 공통: 고객사 설정, 상태 DB, 정책 검사, 원본 데이터 접근, 감사 로그.

고객사 분리: 설정 파일 1개 = 고객사·프로젝트 1개 = 상태 DB 1개. 데이터는 그 설정의
datasets 등록부에 있는 경로로만 읽는다(다른 고객사 파일 경로를 직접 열 수 없다).
"""
import csv, json, os, re, sqlite3, datetime, threading
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # Project K
ALLOW, NEED_APPROVAL, DENY, UNCLEAR = "허용", "승인 필요", "금지", "불명확"
# 첫 버전에서는 어떤 설정으로도 자동 실행하지 않는 행동(요구사항 9, G7-5)
HARD_FORBIDDEN = {"external.publish", "ad.execute", "payment.execute", "message.send", "data.delete", "source.write"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT, updated_by TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, via TEXT, action TEXT, resource TEXT, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS experiments(id INTEGER PRIMARY KEY, status TEXT, data TEXT, gate TEXT, analysis TEXT, parent_id INTEGER, created_by TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, exp_id INTEGER, arm TEXT, data TEXT, source TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, exp_id INTEGER, ts TEXT, actor TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY, exp_id INTEGER, assignee TEXT, kind TEXT, title TEXT, status TEXT, created_at TEXT, closed_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, workflow TEXT, goal TEXT, user TEXT, status TEXT, payload TEXT, started_at TEXT, ended_at TEXT, report_id INTEGER);
CREATE TABLE IF NOT EXISTS agent_tasks(id INTEGER PRIMARY KEY, run_id INTEGER, seq INTEGER, role TEXT, title TEXT, status TEXT, policy TEXT, started_at TEXT, ended_at TEXT, output TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY, name TEXT, size INTEGER, sha256 TEXT, content BLOB, status TEXT, detail TEXT, owner_type TEXT, owner_id INTEGER, uploaded_by TEXT, uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS board_archive(id INTEGER PRIMARY KEY, item_key TEXT, category TEXT, title TEXT, meta TEXT, note TEXT, exp_id INTEGER, by_user TEXT, archived_at TEXT, active INTEGER, restored_by TEXT, restored_at TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, run_id INTEGER, created_at TEXT, title TEXT, body TEXT, pdf_path TEXT);
"""


def now():
    """실제 처리 시각(기록용). 업무 기준일은 Tenant.as_of를 쓴다(G1-2)."""
    return datetime.datetime.now().isoformat(timespec="seconds")


def num(v):
    """NONE(해당 없음)은 0이 아니라 None으로 둔다(G3-4)."""
    return None if v in ("NONE", "", None) else float(v)


def dumps(o):
    return json.dumps(o, ensure_ascii=False)


class Tenant:
    def __init__(self, config_path, db_path=None):
        p = Path(config_path).resolve()
        self.cfg = json.loads(p.read_text(encoding="utf-8"))
        self.id = f"{self.cfg['tenant_id']}_{self.cfg['project_id']}"
        # 자료 폴더는 환경변수로 바꿀 수 있다(내 회사 자료를 다른 곳에 둘 때)
        self.data_root = Path(os.environ["PROJECTK_DATA_ROOT"]).resolve() if os.environ.get("PROJECTK_DATA_ROOT") else (p.parent / self.cfg["data_root"]).resolve()
        self.db_path = Path(db_path) if db_path else ROOT / "var" / f"{self.id}.db"
        self._lock, self._cache = threading.Lock(), {}
        # DATABASE_URL이 있고 DB 파일을 따로 지정하지 않았으면 PostgreSQL을 쓴다(컨테이너 재시작에도 자료가 남도록).
        self.pg, self._con = (os.environ.get("DATABASE_URL") if not db_path else None), None
        if self.pg:
            for stmt in SCHEMA.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY").replace("BLOB", "BYTEA").split(";"):
                if stmt.strip():
                    self._pgq(stmt)
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as con:
            con.executescript(SCHEMA)

    @property
    def db_kind(self):
        n = self.db_path.name
        return "PostgreSQL" if self.pg else "시연용 DB" if "demo" in n else "시험 DB" if "test" in n else "운영 DB"

    def _pgq(self, sql, params=()):
        """SQLite용으로 쓴 SQL을 PostgreSQL에서 그대로 돌린다: ? → %s, INSERT는 새 id를 돌려주고, runs.user는 예약어라 따옴표를 붙인다."""
        import psycopg
        from psycopg.rows import dict_row
        sql = sql.replace("%", "%%").replace("?", "%s")
        if re.search(r"\bruns\b", sql):
            sql = re.sub(r"\buser\b", '"user"', sql)
        head = sql.lstrip().upper()
        ins = head.startswith("INSERT") and "INTO SETTINGS" not in head
        if ins:
            sql += " RETURNING id"
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._con is None or self._con.closed:
                        self._con = psycopg.connect(self.pg, autocommit=True, row_factory=dict_row, prepare_threshold=None)
                    cur = self._con.execute(sql, params)
                    if ins:
                        return cur.fetchone()["id"]
                    return [dict(r) for r in cur.fetchall()] if cur.description else None
                except psycopg.OperationalError:  # 끊긴 연결은 한 번 다시 잇는다
                    self._con = None
                    if attempt:
                        raise

    @property
    def as_of(self):
        return self.cfg["as_of"]

    # ---- 상태 DB ----
    def q(self, sql, params=()):
        if self.pg:
            return self._pgq(sql, params)
        with self._lock, closing(sqlite3.connect(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(sql, params)
            out = [dict(r) for r in cur.fetchall()] if cur.description else cur.lastrowid
            con.commit()
            return out

    def get(self, key, default=None):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set(self, key, value, user):
        self.q("INSERT INTO settings VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
               " updated_by=excluded.updated_by, updated_at=excluded.updated_at", (key, dumps(value), user, now()))

    # ---- 정책 ----
    def policy(self):
        return self.get("policy") or self.cfg["policy"]

    def set_policy(self, user, policy):
        """정책 변경은 policy.edit 권한(관리자)만. 변경 전후는 감사 로그와 settings에 남는다."""
        self.require(user, "policy.edit")
        for k, typ in (("forbidden_actions", list), ("approval_required_actions", list), ("auto_actions", list), ("roles", dict), ("external_ai_transfer", bool)):
            if not isinstance(policy.get(k), typ):
                raise ValueError(f"정책 항목 '{k}'가 없거나 형식이 틀림")
        if not any("policy.edit" in r.get("actions", []) for r in policy["roles"].values()):
            raise ValueError("policy.edit 권한을 가진 역할이 하나도 없으면 정책을 다시 바꿀 수 없음")
        self.set("policy", policy, user)

    def role_of(self, user):
        u = self.cfg["users"].get(user)
        return u["role"] if u else user  # 에이전트는 역할명이 곧 actor

    def check(self, actor, action, resource=None, via=None, log=True):
        """실행 전 정책 검사. 반환 decision: 허용 / 승인 필요 / 금지 / 불명확.
        via = 작업을 위임한 주체(총괄). 위임자는 자신이 못 하는 일을 하위에 허용할 수 없다."""
        pol, role_name = self.policy(), self.role_of(actor)
        role = pol["roles"].get(role_name)

        def decide():
            if action in HARD_FORBIDDEN:
                return DENY, "첫 버전 고정 금지 행동(정책 설정으로도 풀 수 없음)"
            if action in pol["forbidden_actions"]:
                return DENY, "금지 행동 목록에 포함"
            if role is None:
                return UNCLEAR, f"'{actor}'의 권한이 정책에 정의되지 않음"
            if action not in role.get("actions", []):
                return DENY, f"'{role_name}'에 '{action}' 권한 없음"
            if action == "data.read":
                ds = role.get("datasets", [])
                if resource not in ds and "*" not in ds:
                    return DENY, f"'{role_name}'의 데이터 접근 범위에 '{resource}' 없음"
            if via:
                d = self.check(via, action, resource, log=False)
                if d["decision"] in (DENY, UNCLEAR):
                    return DENY, f"위임자 '{via}'의 권한 한도 초과: {d['reason']}"
            if action in pol["approval_required_actions"]:
                return NEED_APPROVAL, "승인 필요 행동"
            if action in pol["auto_actions"]:
                return ALLOW, "자동 실행 허용 범위"
            return UNCLEAR, "허용·승인 필요·금지 어디에도 분류되지 않은 행동"

        decision, reason = decide()
        if log:
            self.q("INSERT INTO audit(ts,actor,via,action,resource,decision,reason) VALUES(?,?,?,?,?,?,?)",
                   (now(), actor, via, action, None if resource is None else str(resource), decision, reason))
        return {"decision": decision, "reason": reason, "actor": actor, "action": action, "resource": resource}

    def require(self, actor, action, resource=None, via=None):
        d = self.check(actor, action, resource, via)
        if d["decision"] != ALLOW:
            raise PermissionError(f"[{d['decision']}] {action}: {d['reason']}")
        return d

    # ---- 원본 데이터 접근(읽기 전용, 접근 시점에 권한 검사) ----
    def rows(self, actor, dataset, via=None):
        self.require(actor, "data.read", dataset, via)
        if dataset not in self._cache:
            spec = self.cfg["datasets"].get(dataset)
            if not spec:
                raise KeyError(f"설정에 등록되지 않은 데이터셋: {dataset}")
            base = ROOT / "data_ext" if spec.get("root") == "ext" else self.data_root
            with open(base / spec["path"], encoding="utf-8-sig", newline="") as f:
                self._cache[dataset] = list(csv.DictReader(f, delimiter="|"))
        return self._cache[dataset]

    def src(self, *datasets):
        return [self.cfg["datasets"][d]["path"] for d in datasets]
