"""
Fixture only — exercises Python match/case dispatch-arm sub-chunking
(_chunk_dispatch_arms, 3 arms meets _MIN_ARMS_FOR_SUBCHUNK) and regex-fallback
top-level boundaries (_PY_TOP). Imported and called by main.py so
import_extractor.py/symbol_extractor.py have a real cross-file relationship
to resolve in call-graph tests.
"""


def route(event_type: str, payload: dict) -> str:
    match event_type:
        case "order.created":
            return handle_order_created(payload)
        case "order.cancelled":
            return handle_order_cancelled(payload)
        case "user.registered":
            return handle_user_registered(payload)
        case _:
            return "unhandled"


def handle_order_created(payload: dict) -> str:
    return f"order created: {payload}"


def handle_order_cancelled(payload: dict) -> str:
    return f"order cancelled: {payload}"


def handle_user_registered(payload: dict) -> str:
    return f"user registered: {payload}"
