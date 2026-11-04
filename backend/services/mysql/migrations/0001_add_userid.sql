-- 为现有用户表补充实际登录用户 ID；可重复执行。
SET @has_userid := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'xp_user_card_simple'
    AND COLUMN_NAME = 'userid'
);

SET @sql := IF(
  @has_userid = 0,
  'ALTER TABLE `xp_user_card_simple` ADD COLUMN `userid` varchar(64) CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci DEFAULT NULL COMMENT ''实际登录用户ID'' AFTER `is_inner_user`',
  'SELECT 1'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
