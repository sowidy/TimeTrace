-- PcTime 服务端表结构（旧库名仍可通过环境变量接入）
--
-- 这份文件是**唯一的表结构定义**：服务端启动时会读它来自动建表
-- （见 db/conn.py 的 init_schema），所以改了这里就等于改了自动建表行为，
-- 不需要再去改 Python 代码。
--
-- 也可以手工执行：
--     mysql -h 127.0.0.1 -u root -p pctime < schema.sql
-- 注意要先自己建库（本文件不含 CREATE DATABASE，因为库名是可配置的）：
--     CREATE DATABASE pctime DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--
-- 所有语句都是 IF NOT EXISTS，重复执行不会报错、也不会动已有数据。
--
-- ⚠️ 改这个文件时别在注释里写分号：db/conn.py 是按分号把文件切开再逐条执行的，
--    注释里的分号会让它切错位置。

CREATE TABLE IF NOT EXISTS `users` (
  `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_key`       CHAR(36)        NOT NULL COMMENT '客户端首次运行生成的 UUID, 同时充当该用户的访问凭据',
  `email`          VARCHAR(255)        NULL COMMENT '订阅邮件地址, 可空表示没填',
  `subscribed`     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否订阅每日邮件',
  `hostname`       VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '机器名, 方便在后台认出是哪台',
  `platform`       VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '操作系统标识',
  `client_version` VARCHAR(32)     NOT NULL DEFAULT '' COMMENT '客户端版本号',
  `created_at`     DATETIME        NOT NULL COMMENT '这个 user_key 第一次出现的时间',
  `last_seen_at`   DATETIME        NOT NULL COMMENT '最近一次上传的时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_key` (`user_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='一个 user_key 一行, 代表一台装了客户端的机器';

CREATE TABLE IF NOT EXISTS `device_tokens` (
  `user_key` CHAR(36) NOT NULL PRIMARY KEY,
  `token_hash` CHAR(64) NOT NULL,
  `created_at` DATETIME NOT NULL,
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `website_usage_records` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL,
  `host` VARCHAR(300) NOT NULL, `display_name` VARCHAR(255) NOT NULL DEFAULT '',
  `seconds` DOUBLE NOT NULL, `uploaded_at` DATETIME NOT NULL,
  UNIQUE KEY `uk_site` (`user_key`,`day`,`host`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `website_hourly_usage` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL,
  `hour` TINYINT UNSIGNED NOT NULL, `host` VARCHAR(300) NOT NULL,
  `seconds` DOUBLE NOT NULL, `uploaded_at` DATETIME NOT NULL,
  UNIQUE KEY `uk_site_hour` (`user_key`,`day`,`hour`,`host`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `website_intervals` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL,
  `host` VARCHAR(300) NOT NULL,
  `start_second` DOUBLE NOT NULL, `end_second` DOUBLE NOT NULL,
  `uploaded_at` DATETIME NOT NULL,
  KEY `idx_site_interval_day` (`user_key`,`day`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `app_timeline_bins` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL,
  `bucket` TINYINT UNSIGNED NOT NULL, `app_name` VARCHAR(255) NOT NULL,
  `seconds` DOUBLE NOT NULL, `uploaded_at` DATETIME NOT NULL,
  UNIQUE KEY `uk_timeline` (`user_key`,`day`,`bucket`,`app_name`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `app_timeline_intervals` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL,
  `app_name` VARCHAR(255) NOT NULL,
  `start_second` DOUBLE NOT NULL, `end_second` DOUBLE NOT NULL,
  `uploaded_at` DATETIME NOT NULL,
  KEY `idx_app_interval_day` (`user_key`,`day`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `android_daily_stats` (
  `user_key` CHAR(36) NOT NULL,
  `day` DATE NOT NULL COMMENT '同步日, 自然日',
  `unlocks` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当天解锁次数, 由安卓在同步时刻实时统计, 与旧汇总 launches 的按应用启动次数不同',
  `uploaded_at` DATETIME NOT NULL,
  PRIMARY KEY (`user_key`,`day`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='安卓设备每日解锁次数, 随时间线上传写入, 重复上传整行覆盖不累加';

CREATE TABLE IF NOT EXISTS `tracking_uploads` (
  `user_key` CHAR(36) NOT NULL, `day` DATE NOT NULL, `kind` VARCHAR(16) NOT NULL,
  `complete` BOOLEAN NOT NULL DEFAULT FALSE, `uploaded_at` DATETIME NOT NULL,
  PRIMARY KEY (`user_key`,`day`,`kind`),
  FOREIGN KEY (`user_key`) REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `usage_records` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_key`     CHAR(36)        NOT NULL,
  `day`          DATE            NOT NULL COMMENT '数据属于哪一天',
  `app_name`     VARCHAR(255)    NOT NULL COMMENT '归一化后的进程名, 如 pycharm64.exe',
  `display_name` VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '界面/邮件里显示的中文名',
  `seconds`      DOUBLE          NOT NULL DEFAULT 0 COMMENT '当天该应用累计使用秒数',
  `launches`     INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '当天启动次数',
  `uploaded_at`  DATETIME        NOT NULL COMMENT '这一行最后一次被写入的时间',
  PRIMARY KEY (`id`),
  -- 这个唯一键是「同一天同一个应用只可能有一行」的硬保证。
  -- 写入本身走的是「先按 (user_key, day) 删、再整批插」（见 db/usage_records.py 的
  -- replace_day），这个键是兜底：就算哪天并发写重了，数据库也会直接
  -- 拒绝第二条重复行，而不是悄悄多记一份时长。
  UNIQUE KEY `uk_user_day_app` (`user_key`, `day`, `app_name`),
  KEY `idx_day` (`day`),
  KEY `idx_user_day` (`user_key`, `day`),
  CONSTRAINT `fk_usage_user` FOREIGN KEY (`user_key`)
      REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='每日应用使用明细, 服务端所有统计都由它聚合出来';

CREATE TABLE IF NOT EXISTS `mail_log` (
  `id`        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_key`  CHAR(36)        NOT NULL,
  `day`       DATE            NOT NULL COMMENT '报告是哪一天的',
  `weekly`    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '0 日报 / 1 周报：同一天可以各发一封',
  `email`     VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '实际发到了哪个地址',
  `status`    VARCHAR(16)     NOT NULL COMMENT 'sent 成功 / failed 失败',
  `detail`    VARCHAR(512)    NOT NULL DEFAULT '' COMMENT '成功时是主题, 失败时是错误信息',
  `sent_at`   DATETIME        NOT NULL COMMENT '最后一次尝试发送的时间',
  PRIMARY KEY (`id`),
  -- 这个唯一键是「同一台设备的同一天同一种报告只发一封」的硬保证。
  -- mail/sender.py 发送前会先查一次, 但那个「查了再发」在进程重启、多 worker
  -- 或者手工触发和定时器撞车时都可能被插队, 所以最终靠数据库这条唯一键
  -- 兜底。失败会覆盖成 sent, 所以「失败重试成功」也只会留下最后一次。
  UNIQUE KEY `uk_mail_user_day_weekly` (`user_key`, `day`, `weekly`),
  KEY `idx_mail_day` (`day`),
  CONSTRAINT `fk_mail_user` FOREIGN KEY (`user_key`)
      REFERENCES `users` (`user_key`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='每日邮件发送记录, 兼作「这一天发过没有」的标记';
