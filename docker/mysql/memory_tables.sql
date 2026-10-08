-- DeepsearchAgent 长期记忆（L3）独立库

SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE DATABASE IF NOT EXISTS deepsearch_memory
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;
USE deepsearch_memory;

-- ===== 事实表：纯追加，失效用软删除（invalid_at + superseded_by），不物理删 =====
-- superseded_by 为空而 invalid_at 非空 = 撤回；superseded_by 非空 = 被新事实取代。
-- 两者在库上可区分，「撤回」与「容量淘汰」则只能靠日志区分（已知并接受的取舍）。
CREATE TABLE IF NOT EXISTS t_agent_memory (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    user_id           VARCHAR(64)  NOT NULL COMMENT '用户维度：长期记忆按用户、不按会话',
    content           VARCHAR(1000) NOT NULL COMMENT '事实正文；单条业务上限由应用侧收敛到 500 字符',
    source_session_id VARCHAR(64)  NULL COMMENT '产生该事实的会话，仅排查用',
    source_from       VARCHAR(64)  NULL COMMENT '素材区间起点（messages 的 ObjectId）',
    source_to         VARCHAR(64)  NULL COMMENT '素材区间终点（messages 的 ObjectId）',
    superseded_by     BIGINT       NULL COMMENT '取代者 id；撤回时留空',
    invalid_at        DATETIME(3)  NULL COMMENT 'NULL = 生效',
    create_time       DATETIME(3)  NOT NULL,
    PRIMARY KEY (id),
    KEY idx_user_valid (user_id, invalid_at),
    KEY idx_superseded (superseded_by)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = 'L3 用户事实表（软失效、纯追加）';

-- ===== 抽取台账：兼水位与审计 =====
-- 水位 = 该用户最后一条「已结算」批次的 to_message_id（ObjectId 单调递增）。
-- 「同用户同时只允许一批在处理」用生成列 + 唯一索引实现：
--   MySQL 8 没有 PostgreSQL 那样的部分唯一索引，而生成列在非 PROCESSING 时为 NULL，
--   NULL 不参与唯一约束，于是历史行可以有很多，PROCESSING 行每个用户最多一条。
CREATE TABLE IF NOT EXISTS t_agent_memory_extraction (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    user_id         VARCHAR(64) NOT NULL,
    from_message_id VARCHAR(64) NOT NULL COMMENT '本批素材起点的 ObjectId',
    to_message_id   VARCHAR(64) NOT NULL COMMENT '本批素材终点的 ObjectId；结算后即为水位',
    from_time       DATETIME(3) NULL,
    to_time         DATETIME(3) NULL,
    status          VARCHAR(16) NOT NULL COMMENT 'PROCESSING / WRITTEN / NOOP / DROPPED / CONFLICT',
    attempt_count   INT         NOT NULL DEFAULT 1,
    decision        TEXT        NULL COMMENT '仲裁输出原文（审计；应用侧无读路径）',
    processing_key  VARCHAR(64) GENERATED ALWAYS AS (IF(status = 'PROCESSING', user_id, NULL)) STORED,
    create_time     DATETIME(3) NOT NULL,
    update_time     DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_processing (processing_key),
    KEY idx_user_batch (user_id, id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = 'L3 抽取台账（水位 + 审计）';

-- ===== 控制面：一用户一行 =====
-- revision 用于仲裁提交时的「双校验」（另一道是水位）：NOOP 批不推版本号，
-- 所以只靠 revision 拦不住重复写入，必须再加水位这一道。
-- create_time 兼作抽取下界：冷启动时取创建时刻，避免把上线前的历史消息全量回灌。
CREATE TABLE IF NOT EXISTS t_agent_memory_control (
    user_id     VARCHAR(64) NOT NULL,
    revision    BIGINT      NOT NULL DEFAULT 0 COMMENT '记忆集版本号',
    create_time DATETIME(3) NOT NULL COMMENT '兼抽取下界（冷启动时刻）',
    update_time DATETIME(3) NOT NULL,
    PRIMARY KEY (user_id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = 'L3 控制面（版本号 + 抽取下界）';

-- ===== 应用用户授权 =====
-- 与业务库的「只读」形成对照：长期记忆必须能写，但写权限**仅限本库**。
-- 刻意不授 CREATE / DROP / ALTER：建表走本脚本（人工执行一次），应用不带 DDL 权限。
-- GRANT 幂等，可随本脚本重复执行。
GRANT SELECT, INSERT, UPDATE, DELETE ON deepsearch_memory.* TO 'AXW'@'%';
FLUSH PRIVILEGES;
