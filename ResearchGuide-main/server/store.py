# -*- coding: utf-8 -*-
"""演示版存储层：SQLite（标准库 sqlite3），单文件零运维。

表：users / facts / tasks / submissions / messages / portraits / projects，
研读层 triage / cards，定位层 edges / statements / bets。
正式版将迁移到 SQLModel + Alembic（见 docs/ARCHITECTURE.md），
但表结构与本文件的字段一一对应，迁移成本可控。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from schemas import MicroTask, UserFact, new_id, now_iso

# 部署时用 QIYAN_DB 指到持久盘（Dockerfile 指向卷 /data/qiyan.db）；不设就用仓库里的演示库。
# 原来写死在 server/data/demo.db，Dockerfile 设的变量没人读，重建容器就丢光所有用户。
DB_PATH = Path(os.environ.get("QIYAN_DB") or Path(__file__).resolve().parent / "data" / "demo.db")
_LOCK = threading.Lock()


def ephemeral() -> bool:
    """库放在会被清掉的地方：/tmp 下，或者在函数计算上却没设 QIYAN_DB（默认路径在代码包里，实例回收就没了）。
    只看路径，不碰磁盘。/api/health 把它报出来，线上一眼能看到。"""
    path = str(DB_PATH).replace("\\", "/")
    if path.startswith("/tmp/") or path.startswith("/private/tmp/"):
        return True
    return bool(os.environ.get("FC_FUNCTION_NAME")) and not os.environ.get("QIYAN_DB")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  nickname TEXT NOT NULL,
  token TEXT NOT NULL,
  onboard_state TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  category TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL,
  source TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  valid_until TEXT,
  affects TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  round TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portraits (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  snapshot TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'picked',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage (
  user_id TEXT NOT NULL,
  kit_id TEXT NOT NULL,
  arxiv_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  why TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, kit_id, arxiv_id)
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kit_id TEXT NOT NULL,
  arxiv_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
DROP INDEX IF EXISTS idx_cards_user;
-- 「每篇最新一版」是按 (用户, 工具包, 论文) 取 MAX(version)；索引带上 version，否则每次都扫全部历史
CREATE INDEX IF NOT EXISTS idx_cards_ver ON cards(user_id, kit_id, arxiv_id, version);
CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  evidence_url TEXT NOT NULL DEFAULT '',
  ref TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS statements (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kit_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  x_ref TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_user ON edges(user_id);
DROP INDEX IF EXISTS idx_statements_user;
CREATE INDEX IF NOT EXISTS idx_statements_ver ON statements(user_id, kit_id, version);
CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id);
-- 对话内核（任务 2）：会话 / 行动 / 事件 / 事实修订 / 决策日志
-- 见 docs/DIALOGUE_CONTRACT.md。这些表与 facts/messages 一样属于「当前活跃画像」，
-- 切换画像时由 _dump_live / _clear_live / _load_live 一起收进快照并清空。
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  topic_hint TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  title TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT '',
  node_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  based_on TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'offered',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_revisions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  evidence TEXT NOT NULL DEFAULT '[]',
  decision_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT '',
  memory_version INTEGER NOT NULL DEFAULT 0,
  move TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  rationale_refs TEXT NOT NULL DEFAULT '[]',
  tool_calls TEXT NOT NULL DEFAULT '[]',
  rejected_ops TEXT NOT NULL DEFAULT '[]',
  degraded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 成绩单：一门课一条记录。
-- 为什么不塞进 facts 的 key-value：一个人 20~40 门课、每门四个字段，
-- 塞进 value 会变成一坨文本（正是 base:code 那起事故的形态），
-- 既撑不住引文校验，也没法按学期/成绩/关键词查。
-- facts 里只放**从成绩单推出来的能力结论**，这里放底稿。
CREATE TABLE IF NOT EXISTS enrollments (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  course TEXT NOT NULL DEFAULT '',
  grade TEXT NOT NULL DEFAULT '',      -- 百分制分数，或 W/I/合格/不合格/缓考/退课
  credits REAL NOT NULL DEFAULT 0,
  term TEXT NOT NULL DEFAULT '',       -- 形如 25-26学年度1学期
  kind TEXT NOT NULL DEFAULT '',       -- 课程性质：专业必修/任选/通选课…
  status TEXT NOT NULL DEFAULT 'completed',  -- completed 已修 | current 在修 | audit 旁听
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 账号：会话只存令牌的哈希，库泄露了也拿不到能用的令牌。
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at REAL NOT NULL,
  last_seen REAL NOT NULL
);
-- 微信登录的一次登录请求：网页拿 ticket（只存哈希）和一个 6 位数字；学生在公众号里发这个数字，
-- 消息回调把发信人的 openid 记到这一行；网页轮询到了就换成会话，这一行作废。
CREATE TABLE IF NOT EXISTS wechat_tickets (
  ticket_hash TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  expires_at REAL NOT NULL,
  openid TEXT,
  guest_uid TEXT,
  nickname TEXT NOT NULL DEFAULT '',
  consent INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wechat_code ON wechat_tickets(code);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_enroll_user ON enrollments(user_id);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_msgs_user ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_portraits_user ON portraits(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_actions_user ON actions(user_id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_revisions_user ON fact_revisions(user_id);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id);

-- 注册名 / 每人一把模型密钥 / 工具包与草稿进度：都在这一份库里按 user_id 隔离。
CREATE TABLE IF NOT EXISTS user_secrets (
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'llm',
  wrapped TEXT NOT NULL,
  tail TEXT NOT NULL DEFAULT '',
  base_url TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  key_id TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (user_id, kind)
);
CREATE TABLE IF NOT EXISTS user_prefs (
  user_id TEXT PRIMARY KEY,
  kit_id TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  user_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, task_id)
);
CREATE TABLE IF NOT EXISTS trails (
  user_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_secrets_user ON user_secrets(user_id);
CREATE INDEX IF NOT EXISTS idx_drafts_user ON drafts(user_id);
CREATE INDEX IF NOT EXISTS idx_trails_user ON trails(user_id);
CREATE TABLE IF NOT EXISTS paper_pool (
  key TEXT NOT NULL,
  day TEXT NOT NULL,
  data TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (key, day)
);
CREATE TABLE IF NOT EXISTS paper_dir (
  node_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  topic_id TEXT NOT NULL,
  rank INTEGER NOT NULL,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '[]',
  published TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  categories TEXT NOT NULL DEFAULT '[]',
  collected_at TEXT NOT NULL,
  PRIMARY KEY (node_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_dir_paper ON paper_dir(paper_id);
CREATE TABLE IF NOT EXISTS tutor_sessions (
  user_id TEXT NOT NULL,
  kit_id TEXT NOT NULL,
  arxiv_id TEXT NOT NULL,
  status TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, kit_id, arxiv_id)
);
CREATE INDEX IF NOT EXISTS idx_tutor_user ON tutor_sessions(user_id);
CREATE TABLE IF NOT EXISTS tutor_questions (
  arxiv_id TEXT NOT NULL,
  station TEXT NOT NULL,
  paper_sha TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  skill_sha TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (arxiv_id, station, paper_sha, plan_version, skill_sha)
);
CREATE TABLE IF NOT EXISTS paper_zh (
  key TEXT PRIMARY KEY,
  title_zh TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 0,
  tried_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS llm_usage (
  user_id TEXT NOT NULL,
  day TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS paper_text (
  arxiv_id TEXT PRIMARY KEY,
  sha TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  tables TEXT NOT NULL DEFAULT '[]',
  retry_after REAL NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_text_fetched ON paper_text(pinned, fetched_at);
CREATE TABLE IF NOT EXISTS user_papers (
  user_id TEXT NOT NULL,
  pid TEXT NOT NULL,
  sha TEXT NOT NULL,
  title TEXT,
  filename TEXT,
  text TEXT NOT NULL,
  pages INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, pid)
);
CREATE TABLE IF NOT EXISTS paper_collect_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  node TEXT NOT NULL DEFAULT '',
  phase TEXT NOT NULL DEFAULT '',
  attempt INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  lock_until REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT ''
);
"""

# 老库补列：SQLite 的 ADD COLUMN 是安全操作，已存在就跳过。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("users", "memory_version", "ALTER TABLE users ADD COLUMN memory_version INTEGER NOT NULL DEFAULT 0"),
    ("messages", "conversation_id", "ALTER TABLE messages ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''"),
    ("facts", "valid_until", "ALTER TABLE facts ADD COLUMN valid_until TEXT"),
    ("actions", "based_on", "ALTER TABLE actions ADD COLUMN based_on TEXT NOT NULL DEFAULT '[]'"),
    ("facts", "affects", "ALTER TABLE facts ADD COLUMN affects TEXT NOT NULL DEFAULT ''"),
    # 信息源边指向 knowledge/channels.json 的 id；早期本地库没有这一列（合并 10-03 的研读/定位层时补上）
    ("edges", "ref", "ALTER TABLE edges ADD COLUMN ref TEXT NOT NULL DEFAULT ''"),
    # 账号：微信 openid（只对我们的公众号有效的编号）、同意隐私说明的时间与版本；
    # claimed_at 为空的是「有账号之前」的老用户，可以凭 uid 认领一次
    ("users", "wechat_openid", "ALTER TABLE users ADD COLUMN wechat_openid TEXT"),
    ("users", "consent_at", "ALTER TABLE users ADD COLUMN consent_at TEXT"),
    ("users", "consent_version", "ALTER TABLE users ADD COLUMN consent_version TEXT NOT NULL DEFAULT ''"),
    ("users", "claimed_at", "ALTER TABLE users ADD COLUMN claimed_at TEXT"),
    ("users", "username", "ALTER TABLE users ADD COLUMN username TEXT"),
    ("users", "password_salt", "ALTER TABLE users ADD COLUMN password_salt TEXT"),
    ("users", "password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
    ("user_secrets", "key_id", "ALTER TABLE user_secrets ADD COLUMN key_id TEXT NOT NULL DEFAULT ''"),
    # 画像自命名：空串表示还没起过名，展示层会退到「按方向起的默认名 / 画像N」
    ("portraits", "name", "ALTER TABLE portraits ADD COLUMN name TEXT NOT NULL DEFAULT ''"),
]

# 依赖补出来的列，必须在补列之后建
_AFTER_MIGRATIONS = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wechat ON users(wechat_openid) WHERE wechat_openid IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username IS NOT NULL AND username != ''",
]


@contextmanager
def _conn():
    """一次调用一个连接：成功提交、异常回滚，最后一定关闭（原来只提交不关闭，连接会一直泄漏）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _LOCK, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")  # 读写不互相阻塞，演示时多开几个页面也不锁库
        c.executescript(_SCHEMA)
        for table, column, ddl in _MIGRATIONS:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                c.execute(ddl)
        for ddl in _AFTER_MIGRATIONS:
            c.execute(ddl)
    migrate_legacy_secrets()


def migrate_legacy_secrets() -> None:
    """把旧 XOR 封装解出来再写成 AES-GCM。失败的行留下，llm_secret 会当没有钥匙。"""
    with _LOCK, _conn() as c:
        rows = list(c.execute("SELECT user_id, kind, wrapped FROM user_secrets"))
        for r in rows:
            wrapped = r["wrapped"] or ""
            if wrapped.startswith("v2:"):
                continue
            try:
                plain = _unwrap_xor(wrapped)
                new = wrap_secret(plain)
            except Exception:
                continue
            c.execute("UPDATE user_secrets SET wrapped=?, key_id='v2' WHERE user_id=? AND kind=?",
                      (new, r["user_id"], r["kind"]))


# ---------- users ----------

def create_user(nickname: str) -> dict[str, Any]:
    """新用户一出生就算「已认领」：只有加账号之前的老用户能凭 uid 认领。
    token 列是早期留下的，从没被校验过；登录凭证是 sessions 表里的会话。"""
    uid, token, now = new_id(), new_id(), now_iso()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO users(id, nickname, token, onboard_state, created_at, claimed_at) VALUES(?,?,?,?,?,?)",
            (uid, nickname, token, "{}", now, now),
        )
    return {"uid": uid, "token": token, "nickname": nickname}


def get_user(uid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,24}$")
PBKDF2_ROUNDS = 210000


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def valid_username(raw: str) -> bool:
    return bool(USERNAME_RE.fullmatch((raw or "").strip()))


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()
    return salt, hashed


def password_ok(password: str, salt: str | None, hashed: str | None) -> bool:
    if not salt or not hashed:
        return False
    _, got = hash_password(password, salt)
    return hmac.compare_digest(got, hashed)


def user_by_username(username: str) -> dict[str, Any] | None:
    name = normalize_username(username)
    if not name:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    return dict(row) if row else None


def register_user(username: str, password: str, nickname: str) -> dict[str, Any]:
    name = normalize_username(username)
    nick = (nickname or username).strip()[:24] or name
    salt, hashed = hash_password(password)
    uid, token, now = new_id(), new_id(), now_iso()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO users(id, nickname, token, onboard_state, created_at, claimed_at, username, password_salt, password_hash)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, nick, token, "{}", now, now, name, salt, hashed),
        )
    return {"uid": uid, "token": token, "nickname": nick, "username": name}


def upgrade_user(uid: str, username: str, password: str) -> dict[str, Any]:
    name = normalize_username(username)
    u = get_user(uid)
    if not u:
        raise KeyError("user")
    if u.get("username"):
        raise ValueError("already")
    salt, hashed = hash_password(password)
    try:
        with _LOCK, _conn() as c:
            cur = c.execute(
                "UPDATE users SET username=?, password_salt=?, password_hash=? "
                "WHERE id=? AND (username IS NULL OR username='')",
                (name, salt, hashed, uid),
            )
            if cur.rowcount != 1:
                raise ValueError("already")
    except sqlite3.IntegrityError as exc:
        raise ValueError("taken") from exc
    out = get_user(uid)
    if not out:
        raise KeyError("user")
    return out


def _wrap_key(secret: str | None = None) -> bytes:
    if secret is None:
        material = os.environ.get("QIYAN_SECRET") or os.environ.get("LLM_API_KEY") or "qiyan-dev-wrap"
    else:
        material = secret
    return hashlib.sha256(material.encode()).digest()


def _aes_key(secret: str | None = None) -> bytes:
    return hashlib.sha256(b"aes:" + _wrap_key(secret)).digest()


def _wrap_xor(plaintext: str, secret: str | None = None) -> str:
    raw = (plaintext or "").encode("utf-8")
    key = _wrap_key(secret)
    out = bytearray(len(raw))
    i = ctr = 0
    while i < len(raw):
        block = hashlib.sha256(key + ctr.to_bytes(4, "big")).digest()
        for b in block:
            if i >= len(raw):
                break
            out[i] = raw[i] ^ b
            i += 1
        ctr += 1
    return out.hex()


def _unwrap_xor(hexed: str, secret: str | None = None) -> str:
    raw = bytes.fromhex(hexed)
    key = _wrap_key(secret)
    out = bytearray(len(raw))
    i = ctr = 0
    while i < len(raw):
        block = hashlib.sha256(key + ctr.to_bytes(4, "big")).digest()
        for b in block:
            if i >= len(raw):
                break
            out[i] = raw[i] ^ b
            i += 1
        ctr += 1
    return bytes(out).decode("utf-8")


def wrap_secret(plaintext: str, secret: str | None = None) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_aes_key(secret)).encrypt(nonce, (plaintext or "").encode("utf-8"), None)
    return "v2:" + nonce.hex() + ":" + ct.hex()


def unwrap_secret(hexed: str, secret: str | None = None) -> str:
    if (hexed or "").startswith("v2:"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _, nonce_h, ct_h = hexed.split(":", 2)
        pt = AESGCM(_aes_key(secret)).decrypt(bytes.fromhex(nonce_h), bytes.fromhex(ct_h), None)
        return pt.decode("utf-8")
    return _unwrap_xor(hexed, secret)


def save_llm_secret(uid: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    tail = api_key[-4:] if len(api_key) >= 4 else api_key
    now = now_iso()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO user_secrets(user_id, kind, wrapped, tail, base_url, model, updated_at, key_id)"
            " VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, kind) DO UPDATE SET "
            "wrapped=excluded.wrapped, tail=excluded.tail, base_url=excluded.base_url, "
            "model=excluded.model, updated_at=excluded.updated_at, key_id=excluded.key_id",
            (uid, "llm", wrap_secret(api_key), tail, base_url, model, now, "v2"),
        )
    return {"has_key": True, "tail": tail, "base_url": base_url, "model": model}


def llm_secret_meta(uid: str) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute(
            "SELECT tail, base_url, model FROM user_secrets WHERE user_id=? AND kind='llm'",
            (uid,),
        ).fetchone()
    if not row:
        return {"has_key": False, "tail": "", "base_url": "", "model": ""}
    return {"has_key": True, "tail": row["tail"], "base_url": row["base_url"], "model": row["model"]}


def llm_secret(uid: str) -> dict[str, str] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT wrapped, base_url, model FROM user_secrets WHERE user_id=? AND kind='llm'",
            (uid,),
        ).fetchone()
    if not row:
        return None
    try:
        key = unwrap_secret(row["wrapped"])
    except Exception as exc:
        from cryptography.exceptions import InvalidTag
        if not isinstance(exc, (InvalidTag, ValueError, UnicodeDecodeError)):
            raise
        print(json.dumps({"event": "llm_secret_invalid", "uid": uid, "error": type(exc).__name__},
                         ensure_ascii=False))
        return None
    if not key:
        return None
    return {"key": key, "base_url": row["base_url"], "model": row["model"]}


def llm_secret_invalid(uid: str) -> bool:
    """密文还在但当前 QIYAN_SECRET 解不开。不解出明文。"""
    with _conn() as c:
        row = c.execute(
            "SELECT wrapped FROM user_secrets WHERE user_id=? AND kind='llm'",
            (uid,),
        ).fetchone()
    if not row:
        return False
    try:
        unwrap_secret(row["wrapped"])
        return False
    except Exception:
        return True


def delete_llm_secret(uid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM user_secrets WHERE user_id=? AND kind='llm'", (uid,))


def get_prefs(uid: str) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT kit_id, data FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    extra: dict[str, Any] = {}
    if row:
        try:
            extra = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        return {"kit_id": row["kit_id"] or "", **extra}
    return {"kit_id": ""}


def set_prefs(uid: str, kit_id: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    cur = get_prefs(uid)
    if kit_id is not None:
        cur["kit_id"] = kit_id
    if extra:
        for k, v in extra.items():
            if k != "kit_id":
                cur[k] = v
    kit = str(cur.get("kit_id") or "")
    blob = {k: v for k, v in cur.items() if k != "kit_id"}
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO user_prefs(user_id, kit_id, data, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET kit_id=excluded.kit_id, data=excluded.data, updated_at=excluded.updated_at",
            (uid, kit, json.dumps(blob, ensure_ascii=False), now_iso()),
        )
    return get_prefs(uid)


def get_draft_row(uid: str, task_id: str) -> dict[str, str]:
    with _conn() as c:
        row = c.execute(
            "SELECT text, updated_at FROM drafts WHERE user_id=? AND task_id=?",
            (uid, task_id),
        ).fetchone()
    if not row:
        return {"text": "", "updated_at": ""}
    return {"text": row["text"] or "", "updated_at": row["updated_at"] or ""}


def get_draft(uid: str, task_id: str) -> str:
    return get_draft_row(uid, task_id)["text"]


def set_draft(uid: str, task_id: str, text: str) -> str:
    return set_draft_row(uid, task_id, text)["text"]


def set_draft_row(uid: str, task_id: str, text: str) -> dict[str, str]:
    text = text or ""
    if not text.strip():
        with _LOCK, _conn() as c:
            c.execute("DELETE FROM drafts WHERE user_id=? AND task_id=?", (uid, task_id))
        return {"text": "", "updated_at": ""}
    stamp = now_iso()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO drafts(user_id, task_id, text, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, task_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (uid, task_id, text, stamp),
        )
    return {"text": text, "updated_at": stamp}


def get_trail(uid: str, project_id: str) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT data FROM trails WHERE user_id=? AND project_id=?", (uid, project_id)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["data"] or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def set_trail(uid: str, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO trails(user_id, project_id, data, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, project_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (uid, project_id, json.dumps(data, ensure_ascii=False), now_iso()),
        )
    return get_trail(uid, project_id)


# ---------- feedback（用户意见反馈） ----------

def add_feedback(uid: str, text: str) -> dict[str, Any]:
    fid = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO feedback(id, user_id, text, status, created_at) VALUES(?,?,?,?,?)",
            (fid, uid, text, "open", now_iso()),
        )
    return {"id": fid, "text": text, "status": "open", "created_at": now_iso()}


def list_feedback(uid: str, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, text, status, created_at FROM feedback"
            " WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_feedback(status: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """给管理端看的全量反馈（带提交人昵称），新→旧。"""
    q = ("SELECT f.id, f.user_id, f.text, f.status, f.created_at,"
         " COALESCE(u.nickname, '') AS nickname"
         " FROM feedback f LEFT JOIN users u ON u.id = f.user_id")
    args: list[Any] = []
    if status:
        q += " WHERE f.status=?"
        args.append(status)
    q += " ORDER BY f.created_at DESC, f.rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def set_feedback_status(fid: str, status: str) -> bool:
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))
        return bool(cur.rowcount)


# ---------- 账号：微信、会话、删号与导出 ----------
# 规则（有效期、频率、签名校验）在 auth.py / wechat.py；这里只管读写。

SESSION_REFRESH = 86400  # 会话一天续一次期，不在每个请求上写库


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_by_openid(openid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE wechat_openid=?", (openid,)).fetchone()
    return dict(row) if row else None


def create_wechat_user(nickname: str, openid: str) -> str:
    """微信新开一个号；同一个 openid 已有账号时抛 sqlite3.IntegrityError。"""
    uid = create_user(nickname)["uid"]
    try:
        with _LOCK, _conn() as c:
            c.execute("UPDATE users SET wechat_openid=? WHERE id=?", (openid, uid))
    except sqlite3.IntegrityError:
        with _LOCK, _conn() as c:
            c.execute("DELETE FROM users WHERE id=?", (uid,))
        raise
    return uid


def bind_wechat(uid: str, openid: str) -> bool:
    """只给还没绑微信的账号（访客、老用户）绑；这个微信已有别的账号时抛 sqlite3.IntegrityError。"""
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE users SET wechat_openid=?, claimed_at=COALESCE(claimed_at, ?)"
                        " WHERE id=? AND wechat_openid IS NULL", (openid, now_iso(), uid))
    return cur.rowcount == 1


def record_consent(uid: str, version: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET consent_at=?, consent_version=? WHERE id=?", (now_iso(), version, uid))


def claim_legacy(uid: str) -> bool:
    """有账号之前的老用户凭 uid 认领一次；认领过、或已经绑了微信的，都不能再凭 uid 进来。"""
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE users SET claimed_at=? WHERE id=? AND claimed_at IS NULL AND wechat_openid IS NULL",
                        (now_iso(), uid))
    return cur.rowcount == 1


def create_session(uid: str, ttl: float) -> str:
    token = "qy_" + secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen) VALUES(?,?,?,?,?)",
                  (_token_hash(token), uid, now_iso(), now + ttl, now))
    return token


def session_lookup(token: str) -> tuple[str | None, bool]:
    """只读：(令牌对应的用户, 是否该续期)。过期的当作没有。
    每个请求都在事件循环里调它，所以不写库；续期和清理交给 touch_session 在别的线程做。"""
    now = time.time()
    with _conn() as c:
        row = c.execute("SELECT user_id, expires_at, last_seen FROM sessions WHERE token_hash=?",
                        (_token_hash(token),)).fetchone()
    if row is None or row["expires_at"] <= now:
        return None, row is not None
    return row["user_id"], now - row["last_seen"] > SESSION_REFRESH


def touch_session(token: str, ttl: float) -> None:
    """用着的会话续期（一天最多一次），过期的删掉。"""
    h, now = _token_hash(token), time.time()
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=? AND expires_at<=?", (h, now))
        c.execute("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?", (now, now + ttl, h))


def session_user(token: str, ttl: float) -> str | None:
    """同步版：查到就顺手续期。给登录接口这类本来就在线程池里跑的地方用。"""
    uid, stale = session_lookup(token)
    if stale:
        touch_session(token, ttl)
    return uid


def drop_session(token: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))



def wechat_ticket_new(ticket: str, code: str, expires_at: float, guest_uid: str | None, nickname: str,
                      consent: bool) -> bool:
    """记一次登录请求。同一个数字已经有人在用（还没过期、没用掉）就返回 False，调用方换一个数字。"""
    now = time.time()
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM wechat_tickets WHERE expires_at < ?", (now - 3600,))  # 过期一小时的顺手清掉
        if c.execute("SELECT 1 FROM wechat_tickets WHERE code=? AND expires_at>? AND used=0", (code, now)).fetchone():
            return False
        c.execute("INSERT INTO wechat_tickets(ticket_hash, code, expires_at, guest_uid, nickname, consent)"
                  " VALUES(?,?,?,?,?,?)", (_token_hash(ticket), code, expires_at, guest_uid, nickname, int(consent)))
    return True


def wechat_claim_code(code: str, openid: str) -> bool:
    """公众号收到一个数字：配上还在等的那次登录请求。微信超时会重发同一条消息，所以重复配同一个 openid 也算成功。"""
    with _LOCK, _conn() as c:
        cur = c.execute("UPDATE wechat_tickets SET openid=? WHERE code=? AND expires_at>? AND used=0"
                        " AND (openid IS NULL OR openid=?)", (openid, code, time.time(), openid))
    return cur.rowcount == 1


def wechat_ticket(ticket: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM wechat_tickets WHERE ticket_hash=?", (_token_hash(ticket),)).fetchone()
    return dict(row) if row else None


def wechat_use_ticket(ticket: str) -> bool:
    """换成会话只能一次：两个轮询同时到，只有一个拿到。过期的不能换。"""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "UPDATE wechat_tickets SET used=1 WHERE ticket_hash=? AND used=0 AND openid IS NOT NULL AND expires_at>?",
            (_token_hash(ticket), time.time()),
        )
    return cur.rowcount == 1


def _user_tables(c: sqlite3.Connection) -> list[str]:
    """所有带 user_id 列的表。按列找而不是写死表名：以后加了新表，删号和导出不会漏。"""
    names = [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [n for n in names if "user_id" in {r["name"] for r in c.execute(f"PRAGMA table_info({n})")}]


def export_user(uid: str) -> dict[str, Any]:
    with _conn() as c:
        user = c.execute("SELECT id, nickname, username, wechat_openid, created_at, consent_at, consent_version, onboard_state"
                         " FROM users WHERE id=?", (uid,)).fetchone()
        out: dict[str, Any] = {"user": dict(user) if user else None}
        for t in _user_tables(c):
            if t == "sessions":
                continue  # 令牌哈希不是用户的数据，导出了也没用
            if t == "user_secrets":
                out[t] = [
                    dict(r) for r in c.execute(
                        "SELECT user_id, kind, tail, base_url, model, updated_at FROM user_secrets WHERE user_id=?",
                        (uid,),
                    )
                ]
                continue
            out[t] = [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE user_id=?", (uid,))]
    return out


def delete_user(uid: str) -> dict[str, int]:
    """真删：这个人在每张表里的行、会话、微信登录记录、账号本身。返回各表删了几行。"""
    counts: dict[str, int] = {}
    with _LOCK, _conn() as c:
        row = c.execute("SELECT wechat_openid FROM users WHERE id=?", (uid,)).fetchone()
        for t in _user_tables(c):
            counts[t] = c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,)).rowcount
        if row and row["wechat_openid"]:
            c.execute("DELETE FROM wechat_tickets WHERE openid=?", (row["wechat_openid"],))
        c.execute("DELETE FROM wechat_tickets WHERE guest_uid=?", (uid,))
        counts["users"] = c.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount
    print(json.dumps({"event": "user_deleted", "uid": uid, "at": now_iso()}, ensure_ascii=False))
    return counts


def set_onboard_state(uid: str, state: dict[str, Any]) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET onboard_state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), uid))


def get_onboard_state(uid: str) -> dict[str, Any]:
    u = get_user(uid)
    if not u:
        return {}
    try:
        return json.loads(u["onboard_state"] or "{}")
    except json.JSONDecodeError:
        return {}


# ---------- facts ----------

def _fact_row(r: sqlite3.Row) -> UserFact:
    return UserFact.from_dict({**dict(r), "evidence": json.loads(r["evidence"] or "[]")})


def add_fact(f: UserFact) -> UserFact:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO facts(id,user_id,category,key,value,confidence,source,evidence,status,valid_until,affects,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.id, f.user_id, f.category, f.key, f.value, f.confidence, f.source,
             json.dumps(f.evidence, ensure_ascii=False), f.status, f.valid_until,
             getattr(f, "affects", "") or "", f.created_at, f.updated_at),
        )
    return f


def list_facts(uid: str, statuses: list[str] | None = None) -> list[UserFact]:
    q = "SELECT * FROM facts WHERE user_id=?"
    args: list[Any] = [uid]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    q += " ORDER BY created_at"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_fact_row(r) for r in rows]


_UNSET = object()


def update_fact(fid: str, value: str | None = None, status: str | None = None,
                valid_until: Any = _UNSET, evidence: list[dict[str, Any]] | None = None) -> UserFact | None:
    """局部更新。valid_until 用哨兵区分「不改」和「清空」，因为 None 是合法值。"""
    f = get_fact(fid)
    if not f:
        return None
    if value is not None:
        f.value = value
    if status is not None:
        f.status = status
    if valid_until is not _UNSET:
        f.valid_until = valid_until  # type: ignore[assignment]
    if evidence is not None:
        f.evidence = evidence
    f.updated_at = now_iso()
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE facts SET value=?, status=?, valid_until=?, evidence=?, updated_at=? WHERE id=?",
            (f.value, f.status, f.valid_until,
             json.dumps(f.evidence, ensure_ascii=False), f.updated_at, fid),
        )
    return f


def retract_fact(fid: str, *, decision_id: str = "") -> UserFact | None:
    """作废一条事实。用户自己在界面上改/删记忆时走这里，留 revision 以便追溯。"""
    f = get_fact(fid)
    if not f:
        return None
    store_evidence = list(f.evidence) + [{"type": "user_edit", "quote": "", "decision_id": decision_id}]
    update_fact(fid, status="retracted", evidence=store_evidence)
    add_revision(f.user_id, fact_id=fid, operation="retract", old_value=f.value,
                 new_value=None, evidence=store_evidence, decision_id=decision_id)
    return get_fact(fid)


def get_fact(fid: str) -> UserFact | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    return _fact_row(row) if row else None


# ---------- tasks ----------

def save_task(t: MicroTask) -> MicroTask:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tasks(id, user_id, data) VALUES(?,?,?)",
            (t.id, t.user_id, json.dumps(t.to_dict(), ensure_ascii=False)),
        )
    return t


def get_task(tid: str) -> MicroTask | None:
    with _conn() as c:
        row = c.execute("SELECT data FROM tasks WHERE id=?", (tid,)).fetchone()
    return MicroTask.from_dict(json.loads(row["data"])) if row else None


def list_tasks(uid: str) -> list[MicroTask]:
    with _conn() as c:
        rows = c.execute("SELECT data FROM tasks WHERE user_id=? ORDER BY rowid", (uid,)).fetchall()
    return [MicroTask.from_dict(json.loads(r["data"])) for r in rows]


# ---------- submissions & messages ----------

def save_submission(task_id: str, uid: str, payload: str) -> str:
    sid = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO submissions(id, task_id, user_id, payload, created_at) VALUES(?,?,?,?,?)",
            (sid, task_id, uid, payload, now_iso()),
        )
    return sid


def add_message(uid: str, role: str, text: str, round_id: str = "", conversation_id: str = "") -> str:
    mid = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO messages(id, user_id, role, text, round, conversation_id, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (mid, uid, role, text, round_id, conversation_id, now_iso()),
        )
    return mid


def list_messages(uid: str, conversation_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM messages WHERE user_id=?"
    args: list[Any] = [uid]
    if conversation_id is not None:
        q += " AND conversation_id=?"
        args.append(conversation_id)
    q += " ORDER BY rowid"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    out = [dict(r) for r in rows]
    return out[-limit:] if limit else out


# ---------- portraits（多份画像；当前份的对话和事实仍写在 live 表里） ----------

def _portrait_label(n: int) -> str:
    d = "零一二三四五六七八九"
    if n < 10:
        return "画像" + d[n]
    if n == 10:
        return "画像十"
    if n < 20:
        return "画像十" + d[n - 10]
    tens, ones = divmod(n, 10)
    return "画像" + d[tens] + "十" + (d[ones] if ones else "")


def _portrait_rows(c: sqlite3.Connection, uid: str) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT * FROM portraits WHERE user_id=? ORDER BY created_at, rowid", (uid,)
    ).fetchall()


def _portrait_public(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {"id": r["id"], "name": r["name"] or "", "fallback": _portrait_label(i),
         "active": bool(r["active"]), "created_at": r["created_at"]}
        for i, r in enumerate(rows, 1)
    ]


def _dump_live(c: sqlite3.Connection, uid: str) -> dict[str, Any]:
    facts = []
    for r in c.execute("SELECT * FROM facts WHERE user_id=? ORDER BY created_at", (uid,)):
        item = dict(r)
        item["evidence"] = json.loads(item.get("evidence") or "[]")
        facts.append(item)
    msgs = [
        {"role": r["role"], "text": r["text"], "round": r["round"],
         "conversation_id": r["conversation_id"], "created_at": r["created_at"]}
        for r in c.execute(
            "SELECT role, text, round, conversation_id, created_at FROM messages"
            " WHERE user_id=? ORDER BY rowid", (uid,))
    ]
    u = c.execute("SELECT onboard_state, memory_version FROM users WHERE id=?", (uid,)).fetchone()
    state: dict[str, Any] = {}
    if u:
        try:
            state = json.loads(u["onboard_state"] or "{}")
        except json.JSONDecodeError:
            state = {}

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in c.execute(sql, (uid,))]

    return {
        "facts": facts,
        "messages": msgs,
        "state": state,
        "memory_version": int(u["memory_version"]) if u else 0,
        # 画像隔离：以下数据同属当前画像。切换/新建画像时必须一起收进快照并在现场清空，
        # 否则新画像会读到旧画像的任务与项目（archive 中的 M5）。
        "tasks": rows("SELECT * FROM tasks WHERE user_id=? ORDER BY rowid"),
        "submissions": rows("SELECT * FROM submissions WHERE user_id=? ORDER BY rowid"),
        "projects": rows("SELECT * FROM projects WHERE user_id=? ORDER BY rowid"),
        "conversations": rows("SELECT * FROM conversations WHERE user_id=? ORDER BY rowid"),
        "actions": rows("SELECT * FROM actions WHERE user_id=? ORDER BY rowid"),
        "events": rows("SELECT * FROM events WHERE user_id=? ORDER BY rowid"),
        "fact_revisions": rows("SELECT * FROM fact_revisions WHERE user_id=? ORDER BY rowid"),
        "decisions": rows("SELECT * FROM decisions WHERE user_id=? ORDER BY rowid"),
        "enrollments": rows("SELECT * FROM enrollments WHERE user_id=? ORDER BY rowid"),
    }


def _clear_live(c: sqlite3.Connection, uid: str) -> None:
    for table in ("facts", "messages", "tasks", "submissions", "projects",
                  "conversations", "actions", "events", "fact_revisions", "decisions",
                  "enrollments"):
        c.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    c.execute("UPDATE users SET onboard_state=?, memory_version=0 WHERE id=?", ("{}", uid))


# 恢复快照时的列顺序；memory_version / degraded 是整数列，缺省补 0，其余补空串。
_RESTORE_COLS: dict[str, tuple[str, ...]] = {
    "tasks": ("id", "user_id", "data"),
    "submissions": ("id", "task_id", "user_id", "payload", "created_at"),
    "projects": ("id", "user_id", "data", "status", "created_at", "updated_at"),
    "conversations": ("id", "user_id", "status", "topic_hint", "created_at", "updated_at"),
    "actions": ("id", "user_id", "conversation_id", "action", "title", "direction",
                "node_id", "payload", "based_on", "status", "created_at", "updated_at"),
    "events": ("id", "user_id", "kind", "source_id", "payload", "created_at"),
    "fact_revisions": ("id", "user_id", "fact_id", "operation", "old_value", "new_value",
                       "evidence", "decision_id", "created_at"),
    "decisions": ("id", "user_id", "conversation_id", "memory_version", "move", "reason",
                  "rationale_refs", "tool_calls", "rejected_ops", "degraded", "created_at"),
    "enrollments": ("id", "user_id", "course", "grade", "credits", "term", "kind",
                    "status", "created_at", "updated_at"),
}
_NUMERIC_COLS = {"memory_version", "degraded", "credits"}


def _restore_rows(c: sqlite3.Connection, table: str, uid: str, items: Any) -> None:
    cols = _RESTORE_COLS[table]
    for item in items or []:
        vals: list[Any] = []
        for col in cols:
            if col == "user_id":
                vals.append(uid)
                continue
            v = item.get(col)
            if v is None:
                v = 0 if col in _NUMERIC_COLS else ""
            vals.append(v)
        c.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(vals),
        )


def _load_live(c: sqlite3.Connection, uid: str, snap: dict[str, Any]) -> None:
    _clear_live(c, uid)
    for f in snap.get("facts") or []:
        c.execute(
            "INSERT INTO facts(id,user_id,category,key,value,confidence,source,evidence,status,valid_until,affects,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.get("id") or new_id(), uid, f.get("category") or "interest", f.get("key") or "",
             f.get("value") or "", float(f.get("confidence") or 0.5), f.get("source") or "declared",
             json.dumps(f.get("evidence") or [], ensure_ascii=False), f.get("status") or "draft",
             f.get("valid_until"), f.get("affects") or "",
             f.get("created_at") or now_iso(), f.get("updated_at") or now_iso()),
        )
    for m in snap.get("messages") or []:
        c.execute(
            "INSERT INTO messages(id, user_id, role, text, round, conversation_id, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (new_id(), uid, m.get("role") or "assistant", m.get("text") or "",
             m.get("round") or "", m.get("conversation_id") or "", m.get("created_at") or now_iso()),
        )
    for table in _RESTORE_COLS:
        _restore_rows(c, table, uid, snap.get(table))
    c.execute(
        "UPDATE users SET onboard_state=?, memory_version=? WHERE id=?",
        (json.dumps(snap.get("state") or {}, ensure_ascii=False),
         int(snap.get("memory_version") or 0), uid),
    )


def _save_active(c: sqlite3.Connection, uid: str) -> None:
    row = c.execute("SELECT id FROM portraits WHERE user_id=? AND active=1", (uid,)).fetchone()
    if not row:
        return
    snap = json.dumps(_dump_live(c, uid), ensure_ascii=False)
    c.execute("UPDATE portraits SET snapshot=? WHERE id=?", (snap, row["id"]))


def active_portrait_id(uid: str) -> str | None:
    for p in list_portraits(uid):
        if p.get("active"):
            return p["id"]
    return None


def list_portraits(uid: str) -> list[dict[str, Any]]:
    with _LOCK, _conn() as c:
        rows = _portrait_rows(c, uid)
        if not rows:
            c.execute(
                "INSERT INTO portraits(id, user_id, snapshot, active, created_at) VALUES(?,?,?,?,?)",
                (new_id(), uid, json.dumps(_dump_live(c, uid), ensure_ascii=False), 1, now_iso()),
            )
            rows = _portrait_rows(c, uid)
        return _portrait_public(rows)


def open_new_portrait(uid: str) -> None:
    """把当前这份收进快照，清空现场，留下一份空的新画像。"""
    with _LOCK, _conn() as c:
        if not _portrait_rows(c, uid):
            c.execute(
                "INSERT INTO portraits(id, user_id, snapshot, active, created_at) VALUES(?,?,?,?,?)",
                (new_id(), uid, json.dumps(_dump_live(c, uid), ensure_ascii=False), 1, now_iso()),
            )
        _save_active(c, uid)
        c.execute("UPDATE portraits SET active=0 WHERE user_id=?", (uid,))
        _clear_live(c, uid)
        c.execute(
            "INSERT INTO portraits(id, user_id, snapshot, active, created_at) VALUES(?,?,?,?,?)",
            (new_id(), uid, "{}", 1, now_iso()),
        )


def activate_portrait(uid: str, pid: str) -> None:
    with _LOCK, _conn() as c:
        target = c.execute("SELECT * FROM portraits WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        if not target:
            raise KeyError(pid)
        if target["active"]:
            return
        _save_active(c, uid)
        try:
            snap = json.loads(target["snapshot"] or "{}")
        except json.JSONDecodeError:
            snap = {}
        _load_live(c, uid, snap)
        c.execute("UPDATE portraits SET active=0 WHERE user_id=?", (uid,))
        c.execute("UPDATE portraits SET active=1 WHERE id=?", (pid,))


def rename_portrait(uid: str, pid: str, name: str) -> None:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "UPDATE portraits SET name=? WHERE id=? AND user_id=?", (name, pid, uid)
        )
        if not cur.rowcount:
            raise KeyError(pid)


def delete_portrait(uid: str, pid: str) -> bool:
    """删一份画像。若删的是最后一份，清空并返回 True，调用方需要重新开场。"""
    with _LOCK, _conn() as c:
        rows = _portrait_rows(c, uid)
        target = next((r for r in rows if r["id"] == pid), None)
        if not target:
            raise KeyError(pid)
        if len(rows) == 1:
            _clear_live(c, uid)
            c.execute("UPDATE portraits SET snapshot=? WHERE id=?", ("{}", pid))
            return True
        if target["active"]:
            other = next(r for r in rows if r["id"] != pid)
            _save_active(c, uid)
            try:
                snap = json.loads(other["snapshot"] or "{}")
            except json.JSONDecodeError:
                snap = {}
            _load_live(c, uid, snap)
            c.execute("UPDATE portraits SET active=0 WHERE user_id=?", (uid,))
            c.execute("UPDATE portraits SET active=1 WHERE id=?", (other["id"],))
        c.execute("DELETE FROM portraits WHERE id=? AND user_id=?", (pid, uid))
        return False


# ---------- projects（边学边练：用户选定的练手项目与每次评阅） ----------

def save_project(uid: str, pid: str, data: dict[str, Any], status: str = "picked") -> dict[str, Any]:
    now = now_iso()
    with _LOCK, _conn() as c:
        row = c.execute("SELECT created_at FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()
        created = row["created_at"] if row else now
        c.execute(
            "INSERT OR REPLACE INTO projects(id, user_id, data, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (pid, uid, json.dumps(data, ensure_ascii=False), status, created, now),
        )
    return {"id": pid, "status": status, "created_at": created, "updated_at": now, **data}


def get_project(uid: str, pid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()
    if not row:
        return None
    return {**json.loads(row["data"]), "id": row["id"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_projects(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (uid,)).fetchall()
    return [{**json.loads(r["data"]), "id": r["id"], "status": r["status"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


# ---------- 研读：每日分拣与阅读卡 ----------

def save_triage(uid: str, kit_id: str, arxiv_id: str, verdict: str, why: str, title: str = "") -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO triage(user_id, kit_id, arxiv_id, verdict, why, title, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, kit_id, arxiv_id, verdict, why, title, now_iso()),
        )


def list_triage(uid: str, kit_id: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM triage WHERE user_id=? AND kit_id=? ORDER BY created_at DESC", (uid, kit_id)).fetchall()
    return [dict(r) for r in rows]


def get_tutor(uid: str, kit_id: str, arxiv_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT status, state FROM tutor_sessions WHERE user_id=? AND kit_id=? AND arxiv_id=?",
                        (uid, kit_id, arxiv_id)).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["state"] or "{}")
    except json.JSONDecodeError:
        state = {}
    return {"status": row["status"], "state": state if isinstance(state, dict) else {}}


def save_tutor(uid: str, kit_id: str, arxiv_id: str, status: str, state: dict[str, Any],
               rev: int | None) -> bool:
    blob = json.dumps(state, ensure_ascii=False)
    now = now_iso()
    with _LOCK, _conn() as c:
        if rev is None:
            c.execute(
                "INSERT INTO tutor_sessions(user_id, kit_id, arxiv_id, status, state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id, kit_id, arxiv_id) DO UPDATE SET "
                "status=excluded.status, state=excluded.state, updated_at=excluded.updated_at",
                (uid, kit_id, arxiv_id, status, blob, now, now),
            )
            return True
        cur = c.execute(
            "UPDATE tutor_sessions SET status=?, state=?, updated_at=? WHERE user_id=? AND kit_id=? AND arxiv_id=? "
            "AND json_extract(state,'$.rev')=?",
            (status, blob, now, uid, kit_id, arxiv_id, rev),
        )
        return cur.rowcount == 1


def get_tutor_questions(arxiv_id: str, station: str, paper_sha: str, plan_version: int, skill_sha: str) -> list | None:
    with _conn() as c:
        row = c.execute(
            "SELECT data FROM tutor_questions WHERE arxiv_id=? AND station=? AND paper_sha=? AND plan_version=? AND skill_sha=?",
            (arxiv_id, station, paper_sha, plan_version, skill_sha),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"] or "[]")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def put_tutor_questions(arxiv_id: str, station: str, paper_sha: str, plan_version: int, skill_sha: str, data: list) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tutor_questions(arxiv_id, station, paper_sha, plan_version, skill_sha, data, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (arxiv_id, station, paper_sha, plan_version, skill_sha, json.dumps(data, ensure_ascii=False), now_iso()),
        )


def list_triage_all(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM triage WHERE user_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    return [dict(r) for r in rows]


def _json_list(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def put_paper_dir(node_id: str, paper_id: str, topic_id: str, rank: int, source: str,
                  title: str, summary: str = "", authors: list | None = None,
                  published: str = "", url: str = "", categories: list | None = None) -> None:
    if not node_id or not paper_id or not title:
        return
    now = now_iso()
    authors_s = json.dumps(authors or [], ensure_ascii=False)
    cats_s = json.dumps(categories or [], ensure_ascii=False)
    last = None
    for _ in range(4):
        try:
            with _LOCK, _conn() as c:
                row = c.execute("SELECT rank FROM paper_dir WHERE node_id=? AND paper_id=?",
                                (node_id, paper_id)).fetchone()
                if row:
                    keep_rank = min(int(row["rank"] or rank), int(rank))
                    old = c.execute(
                        "SELECT authors, url, categories FROM paper_dir WHERE node_id=? AND paper_id=?",
                        (node_id, paper_id)).fetchone()
                    keep_authors = authors_s if (authors or []) else (old["authors"] if old else authors_s)
                    keep_url = url if url else (old["url"] if old else url)
                    keep_cats = cats_s if (categories or []) else (old["categories"] if old else cats_s)
                    c.execute(
                        "UPDATE paper_dir SET topic_id=?, rank=?, source=?, title=?, summary=?, authors=?, "
                        "published=?, url=?, categories=?, collected_at=? WHERE node_id=? AND paper_id=?",
                        (topic_id or "", keep_rank, source or "", title, summary or "", keep_authors,
                         published or "", keep_url, keep_cats, now, node_id, paper_id),
                    )
                else:
                    c.execute(
                        "INSERT INTO paper_dir(node_id, paper_id, topic_id, rank, source, title, summary, "
                        "authors, published, url, categories, collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (node_id, paper_id, topic_id or "", int(rank), source or "", title, summary or "",
                         authors_s, published or "", url or "", cats_s, now),
                    )
            return
        except sqlite3.OperationalError as exc:
            last = exc
            time.sleep(0.25)
    if last:
        raise last


def list_paper_dir(node_id: str) -> list[dict[str, Any]]:
    if not node_id:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM paper_dir WHERE node_id=? ORDER BY rank ASC, paper_id ASC",
            (node_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "node_id": r["node_id"], "paper_id": r["paper_id"], "topic_id": r["topic_id"],
            "rank": r["rank"], "source": r["source"], "title": r["title"],
            "summary": r["summary"] or "", "authors": _json_list(r["authors"]),
            "published": r["published"] or "", "url": r["url"] or "",
            "categories": _json_list(r["categories"]), "collected_at": r["collected_at"],
        })
    return out


def get_paper_dir_item(paper_id: str) -> dict[str, Any] | None:
    if not paper_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM paper_dir WHERE paper_id=? ORDER BY rank ASC LIMIT 1",
            (paper_id,)).fetchone()
    if not row:
        return None
    return {
        "node_id": row["node_id"], "paper_id": row["paper_id"], "topic_id": row["topic_id"],
        "rank": row["rank"], "source": row["source"], "title": row["title"],
        "summary": row["summary"] or "", "authors": _json_list(row["authors"]),
        "published": row["published"] or "", "url": row["url"] or "",
        "categories": _json_list(row["categories"]), "collected_at": row["collected_at"],
    }


def search_paper_dir(q: str, limit: int = 20) -> list[dict[str, Any]]:
    toks = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,30}", q or "") if len(t) >= 2][:6]
    if not toks:
        return []
    where = " AND ".join(["(lower(title) LIKE ? OR lower(summary) LIKE ?)" for _ in toks])
    params: list[str] = []
    for t in toks:
        like = f"%{t}%"
        params.extend([like, like])
    cap = max(1, min(int(limit), 50))
    params.append(cap)
    with _conn() as c:
        rows = c.execute(
            f"SELECT paper_id, MIN(rank) AS rank, MIN(node_id) AS node_id, "
            f"MIN(topic_id) AS topic_id, MIN(source) AS source, MIN(title) AS title, "
            f"MIN(summary) AS summary, MIN(authors) AS authors, MIN(published) AS published, "
            f"MIN(url) AS url, MIN(categories) AS categories "
            f"FROM paper_dir WHERE {where} GROUP BY paper_id ORDER BY rank ASC, paper_id ASC LIMIT ?",
            params).fetchall()
    out = []
    for r in rows:
        pid = r["paper_id"]
        out.append({
            "id": pid, "paper_id": pid, "title": r["title"], "summary": r["summary"] or "",
            "authors": _json_list(r["authors"]), "published": r["published"] or "",
            "url": r["url"] or "", "categories": _json_list(r["categories"]),
            "source": r["source"], "node_id": r["node_id"],
        })
    return out


def paper_dir_counts() -> dict[str, int]:
    with _conn() as c:
        try:
            rows = c.execute("SELECT node_id, COUNT(*) n FROM paper_dir GROUP BY node_id").fetchall()
        except sqlite3.OperationalError:
            return {}
    return {r["node_id"]: int(r["n"]) for r in rows}


def get_paper_pool(key: str, day: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data, fetched_at FROM paper_pool WHERE key=? AND day=?", (key, day)).fetchone()
    if not row:
        return None
    try:
        papers = json.loads(row["data"] or "[]")
    except json.JSONDecodeError:
        return None
    return {"papers": papers if isinstance(papers, list) else [], "fetched_at": row["fetched_at"]}


def latest_paper_pool(key: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT day, data, fetched_at FROM paper_pool WHERE key=? ORDER BY day DESC LIMIT 1", (key,)).fetchone()
    if not row:
        return None
    try:
        papers = json.loads(row["data"] or "[]")
    except json.JSONDecodeError:
        return None
    return {"day": row["day"], "papers": papers if isinstance(papers, list) else [], "fetched_at": row["fetched_at"]}


def get_paper_zh(key: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT key, title_zh, keywords, model, ok, tried_at FROM paper_zh WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        kws = json.loads(row["keywords"] or "[]")
    except json.JSONDecodeError:
        kws = []
    return {"key": row["key"], "title_zh": row["title_zh"] or "", "keywords": kws if isinstance(kws, list) else [],
            "model": row["model"] or "", "ok": int(row["ok"] or 0), "tried_at": row["tried_at"] or ""}


def put_paper_zh(key: str, title_zh: str, keywords: list[str], model: str = "", ok: int = 1,
                 tried_at: str | None = None) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO paper_zh(key, title_zh, keywords, model, ok, tried_at) VALUES(?,?,?,?,?,?)",
            (key, title_zh or "", json.dumps(keywords or [], ensure_ascii=False), model or "", int(ok),
             tried_at or now_iso()),
        )


def get_paper_text(aid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM paper_text WHERE arxiv_id=?", (aid,)).fetchone()
    if not row:
        return None
    try:
        tables = json.loads(row["tables"] or "[]")
    except json.JSONDecodeError:
        tables = []
    return {
        "id": row["arxiv_id"], "title": row["title"], "source": row["source"], "text": row["text"],
        "url": row["url"], "tables": tables, "retry_after": row["retry_after"],
        "sha": row["sha"], "pinned": row["pinned"], "fetched_at": row["fetched_at"],
    }


def put_paper_text(aid: str, result: dict[str, Any], pinned: bool = False, force: bool = False) -> None:
    blob = json.dumps(result.get("tables") or [], ensure_ascii=False)
    text = result.get("text") or ""
    new_src = result.get("source") or "abstract"
    with _LOCK, _conn() as c:
        old = c.execute("SELECT pinned, source FROM paper_text WHERE arxiv_id=?", (aid,)).fetchone()
        keep_pin = 1 if pinned or (old and old["pinned"]) else 0
        if (not force and old and (old["source"] or "") in ("html", "pdf") and new_src == "abstract"):
            if keep_pin and not old["pinned"]:
                c.execute("UPDATE paper_text SET pinned=1 WHERE arxiv_id=?", (aid,))
            return
        c.execute(
            "INSERT OR REPLACE INTO paper_text(arxiv_id, sha, source, title, url, text, tables, retry_after, bytes, pinned, fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, result.get("sha") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
             result.get("source") or "abstract", result.get("title") or "",
             result.get("url") or "", text, blob, float(result.get("retry_after") or 0),
             len(text.encode("utf-8")), keep_pin, now_iso()),
        )
        cap = int(os.environ.get("ARXIV_CACHE_MAX_BYTES") or (100 * 1024 * 1024))
        max_files = int(os.environ.get("ARXIV_CACHE_MAX_FILES") or 2000)
        total = c.execute("SELECT COALESCE(SUM(bytes),0) FROM paper_text").fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM paper_text").fetchone()[0]
        while total > cap or n > max_files:
            old = c.execute("SELECT arxiv_id, bytes FROM paper_text WHERE pinned=0 ORDER BY fetched_at LIMIT 1").fetchone()
            if not old:
                break
            c.execute("DELETE FROM paper_text WHERE arxiv_id=?", (old["arxiv_id"],))
            total -= old["bytes"]
            n -= 1


def pin_papers(ids: list[str]) -> None:
    if not ids:
        return
    with _LOCK, _conn() as c:
        for aid in ids:
            c.execute("UPDATE paper_text SET pinned=1 WHERE arxiv_id=?", (aid,))


def try_collect_lock(seconds: float = 240) -> bool:
    now = time.time()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO paper_collect_state(id, node, phase, attempt, error, lock_until, updated_at) "
            "VALUES(1,'','idle',0,'',0,'')"
        )
        row = c.execute("SELECT lock_until FROM paper_collect_state WHERE id=1").fetchone()
        if row and float(row["lock_until"] or 0) > now:
            return False
        c.execute("UPDATE paper_collect_state SET lock_until=?, updated_at=? WHERE id=1",
                  (now + seconds, now_iso()))
        return True


def release_collect_lock() -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE paper_collect_state SET lock_until=0, updated_at=? WHERE id=1", (now_iso(),))


def collect_progress() -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT * FROM paper_collect_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def set_collect_progress(node: str = "", phase: str = "", attempt: int = 0, error: str = "") -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO paper_collect_state(id, node, phase, attempt, error, lock_until, updated_at) "
            "VALUES(1,'','idle',0,'',0,'')"
        )
        c.execute(
            "UPDATE paper_collect_state SET node=?, phase=?, attempt=?, error=?, updated_at=? WHERE id=1",
            (node or "", phase or "", int(attempt), error or "", now_iso()),
        )


def paper_lib(ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    with _conn() as c:
        rows = c.execute(f"SELECT arxiv_id, source FROM paper_text WHERE arxiv_id IN ({q})", ids).fetchall()
    return {r["arxiv_id"]: r["source"] for r in rows}


def get_user_paper(uid: str, pid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM user_papers WHERE user_id=? AND pid=?", (uid, pid)).fetchone()
    if not row:
        return None
    return {"id": row["pid"], "title": row["title"] or row["filename"] or pid, "text": row["text"],
            "source": "pdf", "url": "", "pages": row["pages"], "filename": row["filename"]}


def list_user_papers(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT pid, title, filename, pages, created_at FROM user_papers WHERE user_id=? ORDER BY created_at DESC",
            (uid,)).fetchall()
    return [dict(r) for r in rows]


def put_user_paper(uid: str, pid: str, sha: str, title: str, filename: str, text: str, pages: int) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_papers(user_id, pid, sha, title, filename, text, pages, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, pid, sha, title, filename, text, pages, now_iso()),
        )
        extras = c.execute(
            "SELECT pid FROM user_papers WHERE user_id=? ORDER BY created_at DESC", (uid,)).fetchall()
        for row in extras[20:]:
            c.execute("DELETE FROM user_papers WHERE user_id=? AND pid=?", (uid, row["pid"]))


def delete_user_paper(uid: str, pid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM user_papers WHERE user_id=? AND pid=?", (uid, pid))
        c.execute("DELETE FROM tutor_sessions WHERE user_id=? AND arxiv_id=?", (uid, pid))


def bump_llm_usage(user_id: str, n: int = 1, day: str | None = None) -> int:
    d = day or now_iso()[:10]
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO llm_usage(user_id, day, calls) VALUES(?,?,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET calls=calls+excluded.calls",
            (user_id, d, n),
        )
        row = c.execute("SELECT calls FROM llm_usage WHERE user_id=? AND day=?", (user_id, d)).fetchone()
    return int(row["calls"] if row else n)


def llm_usage_today(user_id: str | None = None) -> int:
    d = now_iso()[:10]
    with _conn() as c:
        if user_id is None:
            row = c.execute("SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE day=?", (d,)).fetchone()
        else:
            row = c.execute("SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE user_id=? AND day=?", (user_id, d)).fetchone()
    return int(row[0] if row else 0)


def put_paper_pool(key: str, day: str, papers: list[dict[str, Any]]) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO paper_pool(key, day, data, fetched_at) VALUES(?,?,?,?)",
            (key, day, json.dumps(papers, ensure_ascii=False), now_iso()),
        )
        c.execute("DELETE FROM paper_pool WHERE day < ?", (_arxiv_day_offset(-3),))


def _arxiv_day_offset(delta: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=-5, days=delta)).date().isoformat()


def save_card(card: dict[str, Any]) -> None:
    card.setdefault("created_at", now_iso())
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO cards(id, user_id, kit_id, arxiv_id, version, data, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (card["id"], card["uid"], card["kit_id"], card["arxiv_id"], card["version"],
             json.dumps(card, ensure_ascii=False), card["status"], card["created_at"]),
        )


def _card_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [json.loads(r["data"]) for r in rows]


def latest_card(uid: str, kit_id: str, arxiv_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data FROM cards WHERE user_id=? AND kit_id=? AND arxiv_id=? ORDER BY version DESC LIMIT 1",
                        (uid, kit_id, arxiv_id)).fetchone()
    return json.loads(row["data"]) if row else None


def card_history(uid: str, kit_id: str, arxiv_id: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT data FROM cards WHERE user_id=? AND kit_id=? AND arxiv_id=? ORDER BY version DESC",
                         (uid, kit_id, arxiv_id)).fetchall()
    return _card_rows(rows)


def latest_cards(uid: str, kit_id: str) -> list[dict[str, Any]]:
    """每篇论文只取最新一版。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT data FROM cards c WHERE user_id=? AND kit_id=? AND version = "
            "(SELECT MAX(version) FROM cards c2 WHERE c2.user_id=c.user_id AND c2.kit_id=c.kit_id AND c2.arxiv_id=c.arxiv_id) "
            "ORDER BY created_at, rowid", (uid, kit_id)).fetchall()
    return _card_rows(rows)


def _in_chunks(uids: list[str], size: int = 500):
    for i in range(0, len(uids), size):
        yield uids[i:i + size]


def iter_peer_key_parts(uids: list[str]):
    """逐行产出 (user_id, 账本 key 或 None, 边的 ref, 边的文字)：只选这几列、用元组、不在内存里攒整张表。
    账本行 → (uid, key, None, None)；自述边 → (uid, None, ref, text)。"""
    with _conn() as c:
        cur = c.cursor()
        cur.row_factory = None
        for part in _in_chunks(uids):
            marks = ",".join("?" * len(part))
            yield from cur.execute(f"SELECT user_id, COALESCE(NULLIF(key, ''), id), NULL, NULL FROM facts "
                                   f"WHERE source='behavior' AND status='active' AND user_id IN ({marks})", part)
            yield from cur.execute(f"SELECT user_id, NULL, ref, text FROM edges WHERE user_id IN ({marks})", part)


def kit_pool(kit_id: str) -> list[str]:
    """在这个工具包里至少有一张过线阅读卡的用户：竞争地图只数有投入的人。"""
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT user_id FROM cards WHERE kit_id=? AND status='pass'", (kit_id,)).fetchall()
    return [r["user_id"] for r in rows]


# ---------- 定位：边、定位陈述、下注组合 ----------

def add_edge(uid: str, kind: str, text: str, evidence_url: str = "", ref: str = "") -> dict[str, Any]:
    row = {"id": new_id(), "user_id": uid, "kind": kind, "text": text, "evidence_url": evidence_url, "ref": ref, "created_at": now_iso()}
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) "
                  "VALUES(:id,:user_id,:kind,:text,:evidence_url,:ref,:created_at)", row)
    return row


def channel_readers() -> dict[str, set[str]]:
    """每个信息源有哪些用户标了「我常看」。"""
    with _conn() as c:
        rows = c.execute("SELECT user_id, ref FROM edges WHERE kind='source' AND ref != ''").fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["ref"], set()).add(r["user_id"])
    return out


def list_edges(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM edges WHERE user_id=? ORDER BY created_at, rowid", (uid,)).fetchall()
    return [dict(r) for r in rows]


def delete_edge(uid: str, edge_id: str) -> bool:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM edges WHERE id=? AND user_id=?", (edge_id, uid)).rowcount > 0


def save_statement(uid: str, kit_id: str, x_ref: str, data: dict[str, Any]) -> dict[str, Any]:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT MAX(version) AS v FROM statements WHERE user_id=? AND kit_id=?", (uid, kit_id)).fetchone()
        version = (row["v"] or 0) + 1
        rec = {**data, "id": new_id(), "version": version, "kit_id": kit_id, "x_ref": x_ref, "created_at": now_iso()}
        c.execute("INSERT INTO statements(id, user_id, kit_id, version, x_ref, data, created_at) VALUES(?,?,?,?,?,?,?)",
                  (rec["id"], uid, kit_id, version, x_ref, json.dumps(rec, ensure_ascii=False), rec["created_at"]))
    return rec


def latest_statement(uid: str, kit_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data FROM statements WHERE user_id=? AND kit_id=? ORDER BY version DESC LIMIT 1", (uid, kit_id)).fetchone()
    return json.loads(row["data"]) if row else None


def statement_refs(kit_id: str, before: str) -> dict[str, str]:
    """每个用户在 before 之前最新一版陈述指向的 x_ref（竞争地图的需求，滞后计）。"""
    # 先按人取一次截止前的最大版本再连回去；原来的相关子查询对每一行都重扫这个人截止后的大量修订
    with _conn() as c:
        rows = c.execute(
            "SELECT s.user_id, s.x_ref FROM statements s JOIN "
            "(SELECT user_id, MAX(version) AS v FROM statements WHERE kit_id=? AND created_at < ? GROUP BY user_id) m "
            "ON s.user_id = m.user_id AND s.version = m.v WHERE s.kit_id = ?",
            (kit_id, before, kit_id)).fetchall()
    return {r["user_id"]: r["x_ref"] for r in rows}


def save_bet(uid: str, bet: dict[str, Any]) -> dict[str, Any]:
    bet = {**bet, "updated_at": now_iso()}
    bet.setdefault("id", new_id())
    bet.setdefault("created_at", bet["updated_at"])
    with _LOCK, _conn() as c:
        c.execute("INSERT OR REPLACE INTO bets(id, user_id, data, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                  (bet["id"], uid, json.dumps(bet, ensure_ascii=False), bet["status"], bet["created_at"], bet["updated_at"]))
    return bet


def list_bets(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT data FROM bets WHERE user_id=? ORDER BY created_at, rowid", (uid,)).fetchall()
    return [json.loads(r["data"]) for r in rows]
# ---------- 对话内核：会话 / 行动 / 事件 / 事实修订 / 决策日志（任务 2）----------
# 契约见 docs/DIALOGUE_CONTRACT.md。这些数据的可见范围与 facts/messages 一致：
# 都属于「当前活跃画像」，切换画像时随快照一起收走。

def get_memory_version(uid: str) -> int:
    u = get_user(uid)
    return int(u["memory_version"]) if u and u.get("memory_version") is not None else 0


def bump_memory_version(uid: str) -> int:
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET memory_version = memory_version + 1 WHERE id=?", (uid,))
        row = c.execute("SELECT memory_version FROM users WHERE id=?", (uid,)).fetchone()
    return int(row["memory_version"]) if row else 0


def _conv_row(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


def open_conversation(uid: str, topic_hint: str = "") -> dict[str, Any]:
    """取本画像当前打开的会话；没有就新建。"""
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT * FROM conversations WHERE user_id=? AND status='open' ORDER BY rowid DESC LIMIT 1",
            (uid,),
        ).fetchone()
        if row:
            return _conv_row(row)
        now = now_iso()
        cid = new_id()
        c.execute(
            "INSERT INTO conversations(id, user_id, status, topic_hint, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (cid, uid, "open", topic_hint[:200], now, now),
        )
        return {"id": cid, "user_id": uid, "status": "open",
                "topic_hint": topic_hint[:200], "created_at": now, "updated_at": now}


def get_conversation(uid: str, cid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, uid)).fetchone()
    return _conv_row(row) if row else None


def touch_conversation(uid: str, cid: str, topic_hint: str | None = None) -> None:
    with _LOCK, _conn() as c:
        if topic_hint is None:
            c.execute("UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?", (now_iso(), cid, uid))
        else:
            c.execute("UPDATE conversations SET updated_at=?, topic_hint=? WHERE id=? AND user_id=?",
                      (now_iso(), topic_hint[:200], cid, uid))


def close_conversation(uid: str, cid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE conversations SET status='closed', updated_at=? WHERE id=? AND user_id=?",
                  (now_iso(), cid, uid))


def list_conversations(uid: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM conversations WHERE user_id=?"
    args: list[Any] = [uid]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    q += " ORDER BY rowid"
    with _conn() as c:
        return [_conv_row(r) for r in c.execute(q, args).fetchall()]


def _action_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d.get("payload") or "{}")
    d["based_on"] = json.loads(d.get("based_on") or "[]")
    return d


def save_action(uid: str, *, conversation_id: str, action: str, title: str,
                direction: str = "", node_id: str = "", payload: dict[str, Any] | None = None,
                status: str = "offered", action_id: str | None = None,
                based_on: list[str] | None = None) -> dict[str, Any]:
    now = now_iso()
    aid = action_id or new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO actions(id, user_id, conversation_id, action, title, direction,"
            " node_id, payload, based_on, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, uid, conversation_id, action, title, direction, node_id,
             json.dumps(payload or {}, ensure_ascii=False),
             json.dumps(based_on or [], ensure_ascii=False), status, now, now),
        )
    return {"id": aid, "user_id": uid, "conversation_id": conversation_id, "action": action,
            "title": title, "direction": direction, "node_id": node_id,
            "payload": payload or {}, "based_on": based_on or [], "status": status,
            "created_at": now, "updated_at": now}


def get_action(uid: str, aid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM actions WHERE id=? AND user_id=?", (aid, uid)).fetchone()
    return _action_row(row) if row else None


def list_actions(uid: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM actions WHERE user_id=?"
    args: list[Any] = [uid]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    q += " ORDER BY rowid"
    with _conn() as c:
        return [_action_row(r) for r in c.execute(q, args).fetchall()]


def update_action(uid: str, aid: str, status: str) -> dict[str, Any] | None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE actions SET status=?, updated_at=? WHERE id=? AND user_id=?",
                  (status, now_iso(), aid, uid))
    return get_action(uid, aid)


def pending_action(uid: str) -> dict[str, Any] | None:
    """当前唯一非终态行动。同一画像同时最多一个，避免反复追问。"""
    open_states = ["offered", "accepted", "in_progress"]
    acts = list_actions(uid, statuses=open_states)
    return acts[-1] if acts else None


def last_action(uid: str) -> dict[str, Any] | None:
    """最近一张行动卡，**不论状态**（含 completed）。

    为什么需要它：用户在任务区把任务交掉之后回到对话，pending_action 已经是
    None 了（那一步终结了），于是对话里那张卡直接消失——用户看不到「我刚做完
    的那件事」。他说过「按常理来讲不应该是在任务工作区进行处理，然后回到对话吗」，
    这就是那句话背后缺的东西：完成的结果必须有落点。
    """
    acts = list_actions(uid)
    return acts[-1] if acts else None


def abandon_open_actions(uid: str, keep: str | None = None) -> int:
    """把未终结的旧行动置为 abandoned；keep 指定的那个保留。"""
    n = 0
    for a in list_actions(uid, statuses=["offered", "accepted", "in_progress"]):
        if keep and a["id"] == keep:
            continue
        update_action(uid, a["id"], "abandoned")
        n += 1
    return n


def add_event(uid: str, kind: str, source_id: str = "", payload: dict[str, Any] | None = None) -> str:
    eid = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO events(id, user_id, kind, source_id, payload, created_at) VALUES(?,?,?,?,?,?)",
            (eid, uid, kind, source_id, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
        )
    return eid


def list_events(uid: str, kinds: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = "SELECT * FROM events WHERE user_id=?"
    args: list[Any] = [uid]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    q += " ORDER BY rowid DESC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        out.append(d)
    return out[:limit]


def add_revision(uid: str, *, fact_id: str, operation: str, old_value: str | None,
                 new_value: str | None, evidence: list[dict[str, Any]] | None = None,
                 decision_id: str = "") -> str:
    rid = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO fact_revisions(id, user_id, fact_id, operation, old_value, new_value,"
            " evidence, decision_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, uid, fact_id, operation, old_value, new_value,
             json.dumps(evidence or [], ensure_ascii=False), decision_id, now_iso()),
        )
    return rid


def list_revisions(uid: str, fact_id: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM fact_revisions WHERE user_id=?"
    args: list[Any] = [uid]
    if fact_id:
        q += " AND fact_id=?"
        args.append(fact_id)
    q += " ORDER BY rowid"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.get("evidence") or "[]")
        out.append(d)
    return out


def add_decision(uid: str, *, conversation_id: str, memory_version: int, move: str, reason: str,
                 rationale_refs: list[str] | None = None, tool_calls: list[dict[str, Any]] | None = None,
                 rejected_ops: list[dict[str, Any]] | None = None, degraded: bool = False) -> str:
    did = new_id()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO decisions(id, user_id, conversation_id, memory_version, move, reason,"
            " rationale_refs, tool_calls, rejected_ops, degraded, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (did, uid, conversation_id, int(memory_version), move, reason,
             json.dumps(rationale_refs or [], ensure_ascii=False),
             json.dumps(tool_calls or [], ensure_ascii=False),
             json.dumps(rejected_ops or [], ensure_ascii=False),
             1 if degraded else 0, now_iso()),
        )
    return did


def list_decisions(uid: str, limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM decisions WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
                         (uid, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for col in ("rationale_refs", "tool_calls", "rejected_ops"):
            d[col] = json.loads(d.get(col) or "[]")
        d["degraded"] = bool(d.get("degraded"))
        out.append(d)
    return out


# ---------- 成绩单（enrollments）----------
# 底稿层：一门课一条记录。能力结论由 server/transcript.py 从这里推，不在这里写。

def replace_enrollments(uid: str, items: list[dict[str, Any]]) -> int:
    """整表替换某个人的成绩单，返回写入条数。

    为什么要「替换」而不是「逐条追加」：成绩单是**快照**，不是流水。
    用户重贴一份新的成绩单，期望的是它以新为准；逐条追加会让同一门课
    （例如重修、或者重复粘贴）出现两三条，后面的绩点就算重了。
    """
    now = now_iso()
    with _conn() as c:
        c.execute("DELETE FROM enrollments WHERE user_id=?", (uid,))
        for it in items:
            c.execute(
                "INSERT INTO enrollments(id,user_id,course,grade,credits,term,kind,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (it.get("id") or new_id(), uid, str(it.get("course") or "")[:120],
                 str(it.get("grade") or "")[:24], float(it.get("credits") or 0),
                 str(it.get("term") or "")[:40], str(it.get("kind") or "")[:40],
                 str(it.get("status") or "completed"), now, now),
            )
    return len(items)


def add_enrollments(uid: str, items: list[dict[str, Any]]) -> int:
    """追加式写入。给「识别并追加」用：不删原有，只跳过完全重复的条目。"""
    now = now_iso()
    n = 0
    with _conn() as c:
        have = {
            (r["course"], r["term"], r["grade"])
            for r in c.execute(
                "SELECT course, term, grade FROM enrollments WHERE user_id=?", (uid,))
        }
        for it in items:
            key = (str(it.get("course") or ""), str(it.get("term") or ""),
                   str(it.get("grade") or ""))
            if key in have:
                continue
            have.add(key)
            c.execute(
                "INSERT INTO enrollments(id,user_id,course,grade,credits,term,kind,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (it.get("id") or new_id(), uid, str(it.get("course") or "")[:120],
                 str(it.get("grade") or "")[:24], float(it.get("credits") or 0),
                 str(it.get("term") or "")[:40], str(it.get("kind") or "")[:40],
                 str(it.get("status") or "completed"), now, now),
            )
            n += 1
    return n


def list_enrollments(uid: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM enrollments WHERE user_id=? ORDER BY rowid", (uid,)).fetchall()
    return [dict(r) for r in rows]


def delete_enrollment(uid: str, eid: str) -> bool:
    """删一门课。带 user_id 条件，防止删掉别人的记录。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM enrollments WHERE id=? AND user_id=?", (eid, uid))
        return cur.rowcount > 0
