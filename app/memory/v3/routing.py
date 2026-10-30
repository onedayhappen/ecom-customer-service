# 文件说明：按用户迁移状态选择 v3 记忆路径，防止回退到旧遗忘前快照。
"""A migrated user cannot fall back to a stale pre-forgetting snapshot."""
from app.config.settings import settings


def enabled(user, *, store=None, tx=None):
    if settings.memory_engine == 'v3':
        return True
    if tx is not None:
        row = tx.one('SELECT migrated FROM memory_v3_users WHERE user_id=?', (user,))
        return bool(row and row['migrated'])
    if store is not None:
        with store.transaction(read_only=True) as current:
            return enabled(user, tx=current)
    return False
