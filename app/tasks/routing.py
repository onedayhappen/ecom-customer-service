# 文件说明：定义澄清选项中订单引用的识别模式。
"""Order reference pattern used to validate clarification options."""
import re

ORDER = re.compile(r'\bORD-[A-Za-z0-9-]+\b', re.I)
