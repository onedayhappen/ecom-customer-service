"""Create additive support tables before deploying API and workers."""
from app.support.runtime import get_store


if __name__ == "__main__":
    get_store().initialize()
    print("Human support schema is ready")
