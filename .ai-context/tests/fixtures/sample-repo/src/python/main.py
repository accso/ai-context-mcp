"""
Fixture only — imports and calls dispatcher.route(), giving
import_extractor.py/symbol_extractor.py a real import edge and call edge to
resolve (used by analyze_impact / import_edges / call_edges tests).
"""
from dispatcher import route


def main() -> None:
    result = route("order.created", {"order_id": "abc-123"})
    print(result)


if __name__ == "__main__":
    main()
