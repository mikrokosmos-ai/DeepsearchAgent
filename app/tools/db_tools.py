"""
MySQL 数据库查询工具模块（**只读**）

封装数据库查询助手使用的三个 LangChain 工具：

    list_sql_tables     列出真实表名（了解结构的第一步）
    get_table_data      预览单表字段与样例数据（受表名白名单约束）
    execute_sql_query   执行只读 SQL（受语句白名单 + 行数上限约束）
"""

import json
import os
import re

from dotenv import load_dotenv
from langchain_core.tools import tool
from mysql.connector import Error, connect

from app.api.monitor import monitor
from app.core.logger import logger

load_dotenv()

# --- 可配置上限（都有安全默认值，不改 .env 也能生效）------------------------
DEFAULT_MAX_ROWS = 200
DEFAULT_QUERY_TIMEOUT_MS = 15_000


def _int_env(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认值（避免 int('abc') 抛 ValueError 击穿工具）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"[db_tools] 环境变量 {name}={raw!r} 不是整数，已回退默认值 {default}")
        return default
    return value if value > 0 else default


def max_rows() -> int:
    """单次查询返回给模型的最大行数。"""
    return _int_env("SQL_MAX_ROWS", DEFAULT_MAX_ROWS)


def query_timeout_ms() -> int:
    """单条查询的服务端超时（毫秒）。"""
    return _int_env("MYSQL_QUERY_TIMEOUT_MS", DEFAULT_QUERY_TIMEOUT_MS)



# --- 集中读取数据库配置 -----------------------------------------------------
def get_db_config() -> dict:
    """
    从环境变量读取 MySQL 连接配置

    所有数据库工具都通过此函数拿到同一份连接参数，避免每个工具重复读取环境变量。
    :return: mysql.connector.connect 可直接使用的连接参数
    """
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": _int_env("MYSQL_PORT", 3306),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
        "connection_timeout": 5,
    }

    # 去掉未配置的可选项，避免把 None 传给 mysql.connector 造成连接参数异常
    config = {k: v for k, v in config.items() if v is not None}

    required_keys = ["user", "password", "database"]
    missing_keys = [k for k in required_keys if k not in config]
    if missing_keys:
        raise ValueError(f"缺失数据库核心配置：{', '.join(missing_keys)}")

    return config


# ===========================================================================
# 只读护栏
# ===========================================================================
# 允许作为语句开头的关键字（一律小写比较）
ALLOWED_SQL_PREFIXES = ("select", "with", "show", "desc", "describe", "explain")

# 只读语句中**不允许**出现的整词关键字：写操作 / DDL / 权限 / 文件导出 / 事务控制
BLOCKED_SQL_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "replace", "upsert",
        "drop", "truncate", "alter", "create", "rename",
        "grant", "revoke", "call", "execute", "prepare", "deallocate",
        "load", "outfile", "dumpfile", "infile", "into",
        "lock", "unlock", "kill", "shutdown", "flush", "reset", "purge",
        "set", "use", "do", "handler",
        "commit", "rollback", "savepoint", "begin", "start",
    }
)

_WORD_RE = re.compile(r"[a-z_]+")
_LIMIT_KEYWORD_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_$\u4e00-\u9fff]+$")


def strip_sql_noise(sql: str) -> str:
    """
    剔除 SQL 中的注释与字符串/标识符字面量内容。

    两个目的：① 避免字符串或注释里的关键字被误判（如 `WHERE note LIKE '%update%'`）；
            ② 避免用注释把关键字拆开绕过检查（如 `DR/**/OP TABLE t`）。
    :param sql: 原始 SQL
    :return: 仅保留结构字符与关键字的等价文本（字面量位置替换为 ''）
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # 行注释：-- 或 #
        if sql.startswith("--", i) or ch == "#":
            newline = sql.find("\n", i)
            i = n if newline < 0 else newline + 1
            out.append(" ")
            continue

        # 块注释
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue

        # 字符串字面量 / 反引号标识符
        if ch in ("'", '"', "`"):
            quote, j = ch, i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:  # 双写转义
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" '' ")
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(cleaned_sql: str) -> list[str]:
    """按分号切分语句（须先经 strip_sql_noise），丢弃空白段。"""
    return [seg for seg in cleaned_sql.split(";") if seg.strip()]


def check_read_only(sql: str) -> str | None:
    """
    只读护栏。**纯函数**，便于单测。

    :param sql: 模型生成的原始 SQL
    :return: None 表示放行；否则返回中文拒绝原因
    """
    if not isinstance(sql, str) or not sql.strip():
        return "SQL 不能为空。"

    cleaned = strip_sql_noise(sql)
    statements = split_statements(cleaned)
    if len(statements) > 1:
        return "只允许执行单条 SQL（检测到多条语句以分号拼接），已拒绝执行。"

    normalized = statements[0].strip().lower()
    if not normalized:
        return "SQL 不能为空。"

    if not normalized.startswith(ALLOWED_SQL_PREFIXES):
        first_word = normalized.split()[0] if normalized.split() else normalized
        return (
            "只允许执行只读查询（SELECT / WITH / SHOW / DESC / EXPLAIN 开头），"
            f"当前语句以「{first_word}」开头，已拒绝执行。"
        )

    hits = sorted(set(_WORD_RE.findall(normalized)) & BLOCKED_SQL_KEYWORDS)
    if hits:
        return (
            f"检测到非只读关键字：{', '.join(hits)}，已拒绝执行。"
            "本工具只允许查询数据，不允许修改数据或结构。"
        )

    return None


def redact_sql_for_log(sql: str, limit: int = 500) -> str:
    """压平并截断超长 SQL，避免日志/监控事件体过大。"""
    text = " ".join(str(sql).split())
    return text if len(text) <= limit else text[:limit] + "...(已截断)"


# ===========================================================================
# 内部执行辅助
# ===========================================================================
def _format_rows(columns: list[str], rows: list[tuple], truncated: bool, cap: int) -> str:
    """把查询结果序列化成 JSON 文本（None 保留为 null，其余非基础类型转字符串）。"""
    def norm(value):
        if value is None or isinstance(value, (int, float, str, bool)):
            return value
        return str(value)

    payload = {
        "columns": columns,
        "rows": [[norm(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
    if truncated:
        payload["note"] = (
            f"结果超过上限 {cap} 行，已截断。请改用更精确的 WHERE 条件或聚合函数缩小范围。"
        )
    return json.dumps(payload, ensure_ascii=False)


def _apply_statement_timeout(cursor, timeout_ms: int) -> None:
    """设置会话级查询超时（仅对 SELECT 生效）。失败不影响主流程。"""
    try:
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_ms)}")
    except Error as e:
        logger.warning(f"[db_tools] 设置 MAX_EXECUTION_TIME 失败（该 MySQL 可能不支持）：{e}")


def _begin_read_only(conn) -> bool:
    """
    开启只读事务（纵深防御）。返回是否成功 —— 老版本 MySQL 不支持时降级，
    此时仍由 check_read_only 承担拦截职责。
    """
    try:
        conn.start_transaction(readonly=True)
        return True
    except Error as e:
        logger.warning(f"[db_tools] 只读事务不可用，降级为语句级护栏：{e}")
        return False


def _run_query(cursor, sql: str, cap: int) -> str:
    """
    执行只读查询并按上限取数，返回 JSON 文本（无结果集时返回提示语）。

    这里多取 1 行只为判断"是否被截断"；即使 SQL 里没写 LIMIT、或 LIMIT 被注释吞掉，
    也绝不会把整表拉进上下文。
    """
    cursor.execute(sql)
    description = cursor.description
    if not description:
        return f"该语句没有返回结果集，SQL 为：{redact_sql_for_log(sql)}"

    columns = [desc[0] for desc in description]
    rows = cursor.fetchmany(cap + 1)
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    return _format_rows(columns, rows, truncated, cap)


def _ensure_limit(sql: str, cap: int) -> str:
    """
    给没有 LIMIT 的 SELECT/WITH 补一个 LIMIT，让数据库侧先少扫一点。

    换行后再接 LIMIT：如果原 SQL 末尾是 `-- 行注释`，补在下一行才不会被注释吞掉。
    （即便如此，真正的硬上限仍是 `_run_query` 里的 fetchmany。）
    """
    cleaned = strip_sql_noise(sql)
    if _LIMIT_KEYWORD_RE.search(cleaned):
        return sql
    if not cleaned.strip().lower().startswith(("select", "with")):
        return sql
    return sql.rstrip().rstrip(";").rstrip() + f"\nLIMIT {cap + 1}"


def _list_tables(cursor, cap: int) -> list[str]:
    """读取当前库的表名列表（受上限约束，避免超大 schema 撑爆上下文）。"""
    cursor.execute("SHOW TABLES")
    return [row[0] for row in cursor.fetchmany(cap)]


def _error_message(e: Exception) -> str:
    """把异常统一转成面向模型的中文提示。"""
    if isinstance(e, Error):
        return f"查询出现异常：{str(e)}"
    return f"数据库工具内部错误：{type(e).__name__}: {str(e)}"


# ===========================================================================
# 工具一：列出表
# ===========================================================================
@tool
def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    :return: JSON 文本 {"columns":["Tables"],"rows":[[表名],...],"row_count":N,"truncated":bool}
             出现异常时返回中文提示（如「查询出现异常：...」），不会抛出
    """
    try:
        monitor.report_tool(tool_name="数据库表名查询工具：list_sql_tables", args={})
        cap = max_rows()
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                _apply_statement_timeout(cursor, query_timeout_ms())
                _begin_read_only(conn)
                tables = _list_tables(cursor, cap)
    except Exception as e:
        return _error_message(e)

    if not tables:
        return "没有可用的表"
    return _format_rows(
        ["Tables"], [(name,) for name in tables], len(tables) >= cap, cap
    )


# ===========================================================================
# 工具二：预览单表
# ===========================================================================
@tool
def get_table_data(table_name: str) -> str:
    """
    查询指定表的前若干行数据（默认至多 200 行，可用环境变量 SQL_MAX_ROWS 调整）

    调用本工具之前，应先调用 list_sql_tables 完成表名校验。
    本工具的作用：
    1. 完成单表样例数据查询
    2. 为多表查询提供表结构信息和数据格式参考
    :param table_name: 必须是 list_sql_tables 返回过的真实表名
    :return: JSON 文本 {"columns":[...],"rows":[[...],...],"row_count":N,"truncated":bool}
             表名不存在时返回「表「xxx」不存在。可用的表：...」
    """
    try:
        monitor.report_tool(
            tool_name="数据库表数据查询工具：get_table_data",
            args={"table_name": table_name},
        )

        if not isinstance(table_name, str) or not table_name.strip():
            return "table_name 不能为空，请先调用 list_sql_tables 获取真实表名。"
        table_name = table_name.strip()

        cap = max_rows()
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                _apply_statement_timeout(cursor, query_timeout_ms())
                _begin_read_only(conn)

                # 表名白名单校验：只允许查真实存在的表，杜绝把模型输入当 SQL 片段拼接
                available = _list_tables(cursor, cap)
                if table_name not in available:
                    # 兼容模型常写的 `tbl` / db.tbl 形式，统一收敛到裸表名
                    bare = table_name.strip("`").split(".")[-1]
                    if bare in available:
                        table_name = bare
                    else:
                        return (
                            f"表「{table_name}」不存在。可用的表：{', '.join(available) or '（无）'}"
                        )

                if not _SAFE_IDENT_RE.match(table_name):
                    return f"表名「{table_name}」包含非法字符，已拒绝执行。"

                return _run_query(cursor, f"SELECT * FROM `{table_name}` LIMIT {cap + 1}", cap)
    except Exception as e:
        return _error_message(e)


# ===========================================================================
# 工具三：自定义只读 SQL
# ===========================================================================
@tool
def execute_sql_query(query: str) -> str:
    """
    执行**只读**自定义 SQL 查询（SELECT / WITH / SHOW / DESC / EXPLAIN）

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    适合多表关联、筛选、聚合、排序等复杂查询。

    :param query: 要执行的自定义只读 SQL 语句（单条，不要用分号拼接多条）
    :return: JSON 文本 {"columns":[...],"rows":[[...],...],"row_count":N,"truncated":bool}
             被拒绝时返回中文原因
    """
    try:
        # 埋点：记录模型最终生成的 SQL，便于教学时观察是否真的落到了正确表字段上
        monitor.report_tool(
            tool_name="数据库表数据查询工具：execute_sql_query", args={"query": query}
        )

        rejection = check_read_only(query)
        if rejection:
            logger.warning(
                f"[db_tools] 已拒绝非只读 SQL：{redact_sql_for_log(query)}｜原因：{rejection}"
            )
            return rejection

        cap = max_rows()
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                _apply_statement_timeout(cursor, query_timeout_ms())
                _begin_read_only(conn)
                return _run_query(cursor, _ensure_limit(query, cap), cap)
    except Exception as e:
        return _error_message(e)


if __name__ == "__main__":
    # 离线自检：只读护栏与 LIMIT 补齐（不需要数据库即可运行）
    # ⚠️ 必须用模块方式运行，否则 `app` 包不在 sys.path 上：
    #     .venv/Scripts/python.exe -m app.tools.db_tools
    cases = [
        ("SELECT * FROM drugs", None),
        ("select drug_id, name from drugs where update_time > '2026-01-01'", None),
        ("SHOW TABLES", None),
        ("WITH t AS (SELECT 1 AS a) SELECT * FROM t", None),
        ("SELECT * FROM drugs WHERE note LIKE '%please update%'", None),
        ("DELETE FROM drugs", "拒绝"),
        ("DROP TABLE drugs", "拒绝"),
        ("UPDATE drugs SET name='x'", "拒绝"),
        ("SELECT 1; DROP TABLE drugs", "拒绝"),
        ("SELECT * FROM drugs INTO OUTFILE '/tmp/x'", "拒绝"),
        ("TRUNCATE TABLE sales_records", "拒绝"),
        ("DR/**/OP TABLE drugs", "拒绝"),
        ("", "拒绝"),
    ]
    print("=== 只读护栏自检 ===")
    bad = 0
    for sql, expect in cases:
        got = check_read_only(sql)
        ok = (got is None) if expect is None else (got is not None)
        bad += 0 if ok else 1
        print(f"  {'OK ' if ok else 'FAIL'} | {(sql[:50] or '(空)'):52s} -> {got or '放行'}")
    print()
    print("=== LIMIT 补齐 ===")
    print("  ", repr(_ensure_limit("SELECT * FROM drugs", 200)))
    print("  ", repr(_ensure_limit("SELECT * FROM drugs LIMIT 5", 200)))
    print()
    print("=== 汇总 ===", "全部通过" if not bad else f"{bad} 项失败")
