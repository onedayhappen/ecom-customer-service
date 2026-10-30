# 文件说明：通过有界 Redis 队列发送事项唤醒提示，持久状态仍由数据库管理。
"""Optional, bounded Redis wake-ups; no task arguments or execution locks here."""
import re

from app.runtime.concurrency import redis_client


class TaskQueue:
    def __init__(self, url, *, prefix="ecom:tasks", max_hints=1000):
        self.redis = redis_client(url)
        self.key, self.max_hints = prefix + ":wake", max_hints

    def notify(self, job_id):
        if not self.redis or not re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", job_id):
            return False
        try:
            self.redis.eval("""redis.call('LPUSH',KEYS[1],ARGV[1])
                redis.call('LTRIM',KEYS[1],0,tonumber(ARGV[2])-1)
                redis.call('EXPIRE',KEYS[1],3600) return 1""", 1, self.key, job_id, self.max_hints)
            return True
        except Exception:
            return False

    def take(self):
        if self.redis:
            try:
                return self.redis.rpop(self.key)
            except Exception:
                pass
        return None
