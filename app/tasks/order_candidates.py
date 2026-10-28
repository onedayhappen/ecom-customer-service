"""Lazy read adapter for explicitly requested order candidate enrichment."""


def read_orders(*, user, scope):
    # Explicit identity/scope bypass ambient task context and platform defaults.
    # Never paginate, perform writes or construct a second model request here.
    from app.services.commerce.service import service
    return service(user=user, scope=scope).read("find_orders", {})
