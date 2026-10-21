"""Order reference pattern used to validate clarification options."""
import re

ORDER = re.compile(r'\bORD-[A-Za-z0-9-]+\b', re.I)
