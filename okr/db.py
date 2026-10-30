"""
OKR 管理平台 —— 数据库模块
SQLite + WAL 模式，支持多 worker 并发
"""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager

from .paths import DATA_DIR

DB_DIR = str(DATA_DIR)
DB_PATH = os.path.join(DB_DIR, "okr.db")

# ── DDL ─────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    wecom_corp_id   TEXT,
    head_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status          TEXT    NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active','disabled')),
    okr_period_type TEXT    NOT NULL DEFAULT 'monthly'
                            CHECK(okr_period_type IN ('monthly','quarterly')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS departments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    company_id   INTEGER REFERENCES companies(id),
    wecom_department_id INTEGER,
    head_user_id INTEGER,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(company_id, name)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user'
                         CHECK(role IN ('superAdmin','admin','dept_head','user')),
    company_id    INTEGER REFERENCES companies(id),
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    wecom_userid  TEXT,
    phone_verified INTEGER NOT NULL DEFAULT 1,
    wecom_role_managed INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','active','disabled')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS okr_objectives (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    title                TEXT    NOT NULL,
    description          TEXT    DEFAULT '',
    type                 TEXT    NOT NULL CHECK(type IN ('company','department','personal')),
    owner_id             INTEGER NOT NULL REFERENCES users(id),
    company_id           INTEGER REFERENCES companies(id),
    department_id        INTEGER REFERENCES departments(id),
    aligned_objective_id INTEGER REFERENCES okr_objectives(id) ON DELETE SET NULL,
    is_public            INTEGER NOT NULL DEFAULT 1,
    status               TEXT    NOT NULL DEFAULT 'draft'
                                CHECK(status IN ('draft','pending','approved','rejected','active','completed')),
    progress             REAL    NOT NULL DEFAULT 0.0,
    weight               REAL    NOT NULL DEFAULT 1.0,
    period               TEXT    NOT NULL,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS okr_key_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id  INTEGER NOT NULL REFERENCES okr_objectives(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    target_value  REAL    NOT NULL DEFAULT 100.0,
    current_value REAL    NOT NULL DEFAULT 0.0,
    unit          TEXT    NOT NULL DEFAULT '%',
    weight        REAL    NOT NULL DEFAULT 1.0,
    progress      REAL    NOT NULL DEFAULT 0.0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS okr_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id INTEGER NOT NULL REFERENCES okr_objectives(id) ON DELETE CASCADE,
    reviewer_id  INTEGER NOT NULL REFERENCES users(id),
    status       TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected')),
    comment      TEXT    DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_config (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL UNIQUE,
    value       TEXT    NOT NULL DEFAULT '',
    description TEXT    DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sms_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT    NOT NULL,
    code       TEXT    NOT NULL,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT    NOT NULL DEFAULT 'legacy_register',
    request_id TEXT    NOT NULL DEFAULT '',
    expires_at TEXT    NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS idx_users_dept ON users(department_id);
CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_departments_company ON departments(company_id);
CREATE INDEX IF NOT EXISTS idx_objectives_type ON okr_objectives(type);
CREATE INDEX IF NOT EXISTS idx_objectives_owner ON okr_objectives(owner_id);
CREATE INDEX IF NOT EXISTS idx_objectives_dept ON okr_objectives(department_id);
CREATE INDEX IF NOT EXISTS idx_objectives_company ON okr_objectives(company_id);
CREATE INDEX IF NOT EXISTS idx_objectives_period ON okr_objectives(period);
CREATE INDEX IF NOT EXISTS idx_kr_objective ON okr_key_results(objective_id);
CREATE INDEX IF NOT EXISTS idx_reviews_objective ON okr_reviews(objective_id);
CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_codes(phone);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wecom_userid
    ON users(wecom_userid) WHERE wecom_userid IS NOT NULL AND wecom_userid != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_departments_wecom_id
    ON departments(company_id, wecom_department_id) WHERE wecom_department_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS okr_employee_notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    employee_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period        TEXT    NOT NULL,
    note          TEXT    NOT NULL DEFAULT '',
    updated_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(company_id, employee_id, period)
);

CREATE INDEX IF NOT EXISTS idx_employee_notes_board
    ON okr_employee_notes(company_id, period, employee_id);

CREATE TABLE IF NOT EXISTS okr_objective_encouragements (
    objective_id INTEGER NOT NULL REFERENCES okr_objectives(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(objective_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_objective_encouragements_objective
    ON okr_objective_encouragements(objective_id);

CREATE TABLE IF NOT EXISTS okr_monthly_reviews (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id         INTEGER NOT NULL REFERENCES okr_objectives(id) ON DELETE CASCADE,
    year_month           TEXT    NOT NULL,
    self_score           INTEGER DEFAULT NULL
                                 CHECK(self_score IS NULL OR (self_score >= 0 AND self_score <= 100)),
    self_comment         TEXT    NOT NULL DEFAULT '',
    self_submitted_at    TEXT    DEFAULT NULL,
    manager_score        INTEGER DEFAULT NULL
                                 CHECK(manager_score IS NULL OR (manager_score >= 0 AND manager_score <= 100)),
    manager_comment      TEXT    NOT NULL DEFAULT '',
    manager_id           INTEGER DEFAULT NULL REFERENCES users(id),
    manager_submitted_at TEXT    DEFAULT NULL,
    status               TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','self_done','completed')),
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(objective_id, year_month)
);

CREATE INDEX IF NOT EXISTS idx_monthly_reviews_obj    ON okr_monthly_reviews(objective_id);
CREATE INDEX IF NOT EXISTS idx_monthly_reviews_period ON okr_monthly_reviews(year_month);

CREATE TABLE IF NOT EXISTS okr_member_evaluations (
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year_month         TEXT NOT NULL,
    satisfaction_score INTEGER NOT NULL CHECK(satisfaction_score BETWEEN 0 AND 100),
    initiative_score   INTEGER NOT NULL CHECK(initiative_score BETWEEN 0 AND 100),
    redline_factor     INTEGER NOT NULL CHECK(redline_factor IN (0, 1)),
    redline_reason     TEXT NOT NULL DEFAULT '',
    comment            TEXT NOT NULL DEFAULT '',
    reviewer_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    revision           INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(user_id, year_month)
);
CREATE INDEX IF NOT EXISTS idx_member_evaluations_period ON okr_member_evaluations(year_month);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    user_name   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    target_type TEXT    NOT NULL,
    target_id   INTEGER,
    detail      TEXT    DEFAULT '',
    ip          TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id);
"""


def _get_conn() -> sqlite3.Connection:
    """获取一个新的数据库连接（WAL 模式 + foreign_keys）"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    """上下文管理器：获取连接，自动提交/回滚/关闭"""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_multi_company(conn):
    """迁移：为已有数据库添加多公司支持"""
    # 检查是否需要迁移：旧库有 departments 表但没有 company_id 列
    has_departments = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='departments'"
    ).fetchone()
    if not has_departments:
        return  # 全新数据库，executescript 会创建完整 schema

    cols = [c[1] for c in conn.execute("PRAGMA table_info(departments)").fetchall()]
    if "company_id" in cols:
        return  # 已迁移

    print("[OKR] 执行多公司迁移...")

    # 1. 创建 companies 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 2. 创建默认公司
    conn.execute(
        "INSERT OR IGNORE INTO companies (name, status) VALUES ('默认公司', 'active')"
    )
    default_cid = conn.execute(
        "SELECT id FROM companies WHERE name = '默认公司'"
    ).fetchone()[0]

    # 3. 重建 departments 表（去掉 name UNIQUE，改为 UNIQUE(company_id, name)）
    # 必须关闭 FK 检查才能 DROP 被引用的表，需要在事务外设置
    conn.commit()  # 提交前面的操作
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE departments_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company_id INTEGER REFERENCES companies(id),
            head_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, name)
        )
    """)
    conn.execute(f"""
        INSERT INTO departments_new (id, name, company_id, head_user_id, created_at, updated_at)
        SELECT id, name, {default_cid}, head_user_id, created_at, updated_at FROM departments
    """)
    conn.execute("DROP TABLE departments")
    conn.execute("ALTER TABLE departments_new RENAME TO departments")
    conn.commit()

    # 4. users 增加 company_id
    cols = [c[1] for c in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "company_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        conn.execute(
            f"UPDATE users SET company_id = {default_cid} WHERE role != 'superAdmin'"
        )

    # 5. okr_objectives 增加 company_id
    cols = [c[1] for c in conn.execute("PRAGMA table_info(okr_objectives)").fetchall()]
    if "company_id" not in cols:
        conn.execute("ALTER TABLE okr_objectives ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        conn.execute(f"UPDATE okr_objectives SET company_id = {default_cid}")

    # 6. 创建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_departments_company ON departments(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_company ON okr_objectives(company_id)")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    print("[OKR] 多公司迁移完成")


def _migrate_pending_status(conn):
    """迁移：users.status 增加 pending 状态（注册审核）"""
    # 检查 users 表是否存在
    has_users = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not has_users:
        return  # 新库，executescript 会创建完整 schema

    # 检查 CHECK 约束是否已包含 pending（通过查看建表 SQL）
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if create_sql and "pending" in create_sql[0]:
        return  # 已包含 pending

    print("[OKR] 执行注册审核迁移...")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    # 获取当前 users 表的列信息
    cols_info = conn.execute("PRAGMA table_info(users)").fetchall()
    col_names = [c[1] for c in cols_info]
    col_list = ", ".join(col_names)

    conn.execute(f"""
        CREATE TABLE users_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            phone         TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            name          TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'user'
                                 CHECK(role IN ('superAdmin','admin','dept_head','user')),
            company_id    INTEGER REFERENCES companies(id),
            department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
            status        TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','active','disabled')),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users")
    conn.execute("DROP TABLE users")
    conn.execute("ALTER TABLE users_new RENAME TO users")

    # 重建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_dept ON users(department_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id)")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    print("[OKR] 注册审核迁移完成")


def _migrate_period_type(conn):
    """为 companies 表添加 okr_period_type 字段（已有则跳过）"""
    has_companies = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='companies'"
    ).fetchone()
    if not has_companies:
        return
    cols = [c[1] for c in conn.execute("PRAGMA table_info(companies)").fetchall()]
    if "okr_period_type" not in cols:
        conn.execute(
            "ALTER TABLE companies ADD COLUMN okr_period_type TEXT NOT NULL DEFAULT 'monthly'"
        )
        conn.commit()
        print("[OKR] okr_period_type 字段迁移完成")


def _migrate_company_head(conn):
    """Add the company-level OKR reviewer/owner without rebuilding existing data."""
    has_companies = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='companies'"
    ).fetchone()
    if not has_companies:
        return
    cols = [c[1] for c in conn.execute("PRAGMA table_info(companies)").fetchall()]
    if "head_user_id" not in cols:
        conn.execute(
            "ALTER TABLE companies ADD COLUMN head_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
        )
        conn.commit()
        print("[OKR] companies.head_user_id migration complete")


def _migrate_monthly_reviews(conn):
    """确保 okr_monthly_reviews 表存在（已有库的热迁移）"""
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='okr_monthly_reviews'"
    ).fetchone()
    if not has_table:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS okr_monthly_reviews (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                objective_id         INTEGER NOT NULL REFERENCES okr_objectives(id) ON DELETE CASCADE,
                year_month           TEXT    NOT NULL,
                self_score           INTEGER DEFAULT NULL,
                self_comment         TEXT    NOT NULL DEFAULT '',
                self_submitted_at    TEXT    DEFAULT NULL,
                manager_score        INTEGER DEFAULT NULL,
                manager_comment      TEXT    NOT NULL DEFAULT '',
                manager_id           INTEGER DEFAULT NULL REFERENCES users(id),
                manager_submitted_at TEXT    DEFAULT NULL,
                status               TEXT    NOT NULL DEFAULT 'pending'
                                             CHECK(status IN ('pending','self_done','completed')),
                created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(objective_id, year_month)
            );
            CREATE INDEX IF NOT EXISTS idx_monthly_reviews_obj    ON okr_monthly_reviews(objective_id);
            CREATE INDEX IF NOT EXISTS idx_monthly_reviews_period ON okr_monthly_reviews(year_month);
        """)
        conn.commit()
        print("[OKR] okr_monthly_reviews 表创建完成")


def _migrate_quarterly_periods(conn):
    """将已切换为月度模式的公司下 YYYY-Qn 格式 period 转为季度首月 YYYY-MM。
    Q1→01  Q2→04  Q3→07  Q4→10
    仅影响 okr_period_type='monthly' 的公司（或 company_id IS NULL 的超级管理员目标）。
    """
    has_obj = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='okr_objectives'"
    ).fetchone()
    if not has_obj:
        return

    rows = conn.execute(
        """
        SELECT o.id, o.period
        FROM okr_objectives o
        LEFT JOIN companies c ON c.id = o.company_id
        WHERE o.period LIKE '%-Q%'
          AND (o.company_id IS NULL
               OR c.okr_period_type = 'monthly')
        """
    ).fetchall()
    if not rows:
        return

    Q_MONTH = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}
    updated = 0
    for row in rows:
        oid, period = row[0], row[1]
        # 格式示例: '2026-Q2'
        parts = period.split("-Q")
        if len(parts) == 2:
            year, q = parts[0], f"Q{parts[1]}"
            month = Q_MONTH.get(q)
            if month:
                new_period = f"{year}-{month}"
                conn.execute(
                    "UPDATE okr_objectives SET period = ? WHERE id = ?",
                    (new_period, oid),
                )
                updated += 1
    if updated:
        conn.commit()
        print(f"[OKR] 季度→月度 period 格式迁移完成，共更新 {updated} 条记录")


def _migrate_wecom_auth(conn):
    """为企业微信登录、组织同步和短信验号补充兼容字段。"""
    table_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    migrations = {
        "companies": {
            "wecom_corp_id": "ALTER TABLE companies ADD COLUMN wecom_corp_id TEXT",
        },
        "departments": {
            "wecom_department_id": "ALTER TABLE departments ADD COLUMN wecom_department_id INTEGER",
        },
        "users": {
            "wecom_userid": "ALTER TABLE users ADD COLUMN wecom_userid TEXT",
            "phone_verified": "ALTER TABLE users ADD COLUMN phone_verified INTEGER NOT NULL DEFAULT 1",
            "wecom_role_managed": "ALTER TABLE users ADD COLUMN wecom_role_managed INTEGER NOT NULL DEFAULT 0",
        },
        "sms_codes": {
            "user_id": "ALTER TABLE sms_codes ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
            "purpose": "ALTER TABLE sms_codes ADD COLUMN purpose TEXT NOT NULL DEFAULT 'legacy_register'",
            "request_id": "ALTER TABLE sms_codes ADD COLUMN request_id TEXT NOT NULL DEFAULT ''",
        },
    }
    changed = False
    for table, columns in migrations.items():
        if table not in table_names:
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, ddl in columns.items():
            if column in existing:
                continue
            try:
                conn.execute(ddl)
                changed = True
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    if "users" in table_names:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wecom_userid "
            "ON users(wecom_userid) WHERE wecom_userid IS NOT NULL AND wecom_userid != ''"
        )
    if "departments" in table_names:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_departments_wecom_id "
            "ON departments(company_id, wecom_department_id) WHERE wecom_department_id IS NOT NULL"
        )
    conn.commit()
    if changed:
        print("[OKR] 企业微信认证字段迁移完成")


def _repair_progress_snapshots(conn):
    """修复旧数据中 current/target 已更新但 progress 仍为 0 的快照。"""
    repaired_krs = 0
    for row in conn.execute(
        "SELECT id, current_value, target_value, progress FROM okr_key_results"
    ).fetchall():
        target = float(row["target_value"] or 0)
        current = float(row["current_value"] or 0)
        calculated = max(0.0, min(100.0, current / target * 100)) if target > 0 else 0.0
        calculated = round(calculated, 1)
        if abs(float(row["progress"] or 0) - calculated) > 0.05:
            conn.execute(
                "UPDATE okr_key_results SET progress = ? WHERE id = ?",
                (calculated, row["id"]),
            )
            repaired_krs += 1

    repaired_objectives = 0
    rows = conn.execute(
        "SELECT o.id, o.progress, "
        "COALESCE(SUM(kr.progress * kr.weight) / NULLIF(SUM(kr.weight), 0), 0) AS calculated "
        "FROM okr_objectives o LEFT JOIN okr_key_results kr ON kr.objective_id = o.id "
        "GROUP BY o.id"
    ).fetchall()
    for row in rows:
        calculated = round(float(row["calculated"] or 0), 1)
        if abs(float(row["progress"] or 0) - calculated) > 0.05:
            conn.execute(
                "UPDATE okr_objectives SET progress = ? WHERE id = ?",
                (calculated, row["id"]),
            )
            repaired_objectives += 1
    if repaired_krs or repaired_objectives:
        conn.commit()
        print(
            f"[OKR] 进度快照修复完成: KR={repaired_krs}, Objective={repaired_objectives}"
        )


def init_db():
    """初始化数据库：建表 + 迁移 + 创建默认 superAdmin"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = _get_conn()
    try:
        # 先执行迁移（为已有旧库），再建表/索引
        _migrate_multi_company(conn)
        _migrate_pending_status(conn)
        _migrate_period_type(conn)
        _migrate_company_head(conn)
        _migrate_monthly_reviews(conn)
        _migrate_quarterly_periods(conn)
        # is_public 字段迁移
        has_obj = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='okr_objectives'").fetchone()
        _migrate_wecom_auth(conn)
        if has_obj:
            cols = [c[1] for c in conn.execute("PRAGMA table_info(okr_objectives)").fetchall()]
            if "is_public" not in cols:
                conn.execute("ALTER TABLE okr_objectives ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1")
                conn.commit()
                print("[OKR] is_public 字段迁移完成")
            if "weight" not in cols:
                try:
                    conn.execute("ALTER TABLE okr_objectives ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
                    conn.commit()
                    print("[OKR] objectives.weight 字段迁移完成")
                except Exception:
                    pass  # 并发 worker 可能已添加
        conn.executescript(_SCHEMA_SQL)
        _repair_progress_snapshots(conn)

        # 检查是否已有 superAdmin
        row = conn.execute(
            "SELECT id FROM users WHERE role = 'superAdmin' LIMIT 1"
        ).fetchone()
        if not row:
            import bcrypt
            pwd_hash = bcrypt.hashpw(
                (os.environ.get("OKR_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(32)).encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            conn.execute(
                "INSERT INTO users (phone, password_hash, name, role, status) "
                "VALUES (?, ?, ?, 'superAdmin', 'active')",
                ((os.environ.get("OKR_BOOTSTRAP_PHONE") or "13000000000"), pwd_hash, "超级管理员"),
            )
            conn.commit()
            print("[OKR] Bootstrap administrator created; credentials are never logged.")
        conn.commit()
    finally:
        conn.close()


# ── 通用查询辅助 ──────────────────────────────────────────────────────

def dict_from_row(row):
    """sqlite3.Row → dict"""
    if row is None:
        return None
    return dict(row)


def dicts_from_rows(rows):
    """[sqlite3.Row] → [dict]"""
    return [dict(r) for r in rows]
