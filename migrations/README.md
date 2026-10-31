# Database migrations

Alembic 配置位于 `migrations/alembic.ini`。调用方必须通过 `Config.attributes["connection"]` 传入已建立的 SQLAlchemy `Connection`；仓库配置、命令参数和普通环境变量都不承载数据库凭证。`holm_persistence.migrations.upgrade_to_head` 与 `downgrade_to_base` 是两个入口；后者回到 base 后会同时移除空的 Alembic 版本表，使数据库恢复为迁移前的结构。

数据分布在 `core`、`usage`、`telemetry` 三个 Schema。新的 Migration 必须可重入，并遵循 Expand → Migrate → Contract；不可逆或破坏性的变更需要在评审中单独说明。
