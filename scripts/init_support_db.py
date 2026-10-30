# 文件说明：在 API 和后台进程启动前初始化支持库的增量表结构。
"""Create additive support tables before deploying API and workers."""
from app.support.runtime import get_store


if __name__ == "__main__":
    get_store().initialize()
    print("Human support schema is ready")
