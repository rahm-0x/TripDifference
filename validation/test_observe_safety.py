"""
Safety interlock for validation/observe.py — not a formality.

observe.py exists to price real Duffel exchange quotes across every live
order, which makes it one accidental line away from also being able to
confirm one. This test does not care whether the current code looks safe on
a read-through; it greps the actual source file for the concrete strings
that would mean it had grown a path to app.py's confirmation route or to
Duffel's own order-change confirmation call, and fails the build the moment
either appears — including inside a comment, since a copy-pasted example is
just as real a landmine as live code.

If this test ever needs to change to let a legitimate new reference through,
that change is the one to scrutinize hardest in review.
"""

import pathlib

OBSERVE_PATH = pathlib.Path(__file__).parent / "observe.py"

# Concrete, code-shaped strings — not the English word "confirm" or
# "execute", which the module's own docstrings use freely to explain why
# it's safe. Note "/air/order_changes" does NOT match Duffel's quote
# endpoint "/air/order_change_requests" (prices.DuffelPriceSource.price_change
# uses that one, and observe.py is expected to reach it) — the extra "_s"
# before "_requests" makes them distinct substrings on purpose.
FORBIDDEN_REFERENCES = (
    "import app",          # app.py defines the execute() route; never import it
    "from app",
    "app.execute",
    "/execute/",           # the execute endpoint's URL path: /orders/<id>/execute/<action>
    "/air/order_changes",  # order-change creation + confirmation (not order_change_requests)
    "actions/confirm",     # Duffel's confirmation step for any change or cancellation
)


def test_observe_py_exists():
    assert OBSERVE_PATH.is_file(), "validation/observe.py is missing"


def test_observe_has_no_execution_path_reference():
    text = OBSERVE_PATH.read_text()
    hits = [needle for needle in FORBIDDEN_REFERENCES if needle in text]
    assert not hits, (
        "validation/observe.py must never reference the execute endpoint or "
        f"a Duffel order-change confirmation call, but found: {hits}"
    )
