#!/usr/bin/env python3
"""
Stage snapshot for TripDifference -> docs/stage.html (one self-contained file).

Nothing in the report is typed in as a bare assertion. Every component status
carries evidence checks that run against the repo each time this script runs:
a regex that must be found (cited as file:line) or must be absent. If a check
fails — a function was renamed, a gap was closed, a doc line moved — that
component drops to "Unknown" rather than keeping a stale claim. Test counts,
pass/fail, commit activity, and branch state are all computed at run time.

Test counts per component are "tests whose name or body matches the
component's pattern", parsed from the test files with ast. A test can count
toward more than one component; tests matching none are listed as unmapped.

Usage:
    python scripts/stage_report.py                # runs pytest, then writes docs/stage.html
    python scripts/stage_report.py --junit FILE   # reuse a `pytest --junitxml` result
    python scripts/stage_report.py --skip-tests   # pass/fail reported as Unknown

The suite's money-path tests use the real database in POSTGRES_URL (see
README.md), so a default run writes and cleans up test rows there.

Stdlib only.
"""

import argparse
import ast
import datetime as dt
import fnmatch
import html
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "stage.html"
# This script and its output quote the very phrases the checks search for.
SELF = {"scripts/stage_report.py", "docs/stage.html"}

BT, BU, ST, NS, UN = "Built+Tested", "Built+Untested", "Stubbed", "Not Started", "Unknown"
STATUSES = [BT, BU, ST, NS, UN]
WEIGHT = {BT: 1.0, BU: 0.5, ST: 0.25, NS: 0.0}          # Unknown is excluded, not zeroed
RANK = {BT: 0, BU: 1, ST: 2, NS: 3, UN: 4}                # worst-of for pipeline nodes
STATUS_KEY = {BT: "good", BU: "warning", ST: "serious", NS: "critical", UN: "unknown"}
STATUS_ICON = {BT: "✓", BU: "◐", ST: "◌", NS: "✕", UN: "?"}

PIPELINE = ["Booking", "Fare Monitoring", "Reshop Evaluation", "Gate",
            "CONFIRM", "Execution", "Fee Capture"]
GATE_LABEL = {"Gate": "Gate (change_total_amount < 0)"}


# ---------------------------------------------------------------------------
# repo access
# ---------------------------------------------------------------------------

def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


_TRACKED = None


def tracked(pattern="*"):
    """Tracked files plus untracked-but-not-ignored ones, minus this report."""
    global _TRACKED
    if _TRACKED is None:
        files = git("ls-files", "--cached", "--others", "--exclude-standard").split("\n")
        _TRACKED = sorted({f for f in files if f and f not in SELF and (ROOT / f).is_file()})
    return [f for f in _TRACKED if fnmatch.fnmatch(f, pattern)]


_TEXT = {}


def read(path):
    if path not in _TEXT:
        try:
            _TEXT[path] = (ROOT / path).read_text(errors="replace")
        except OSError:
            _TEXT[path] = None
    return _TEXT[path]


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


class E:
    """One evidence check. present: `pattern` found in `path` (cited path:line).
    absent: `pattern` found in none of the files matching `path` (a glob)."""

    def __init__(self, path, pattern, note="", absent=False):
        self.path, self.pattern, self.note, self.absent = path, pattern, note, absent

    def run(self):
        rx = re.compile(self.pattern, re.M)
        if self.absent:
            hits = []
            for f in tracked(self.path):
                text = read(f) or ""
                m = rx.search(text)
                if m:
                    hits.append(f"{f}:{line_of(text, m.start())}")
            return {"ok": not hits, "absent": True, "note": self.note,
                    "cite": f"not found in {self.path}" if not hits else f"FOUND at {', '.join(hits)}"}
        text = read(self.path)
        m = rx.search(text) if text is not None else None
        return {"ok": bool(m), "absent": False, "note": self.note,
                "cite": f"{self.path}:{line_of(text, m.start())}" if m else f"{self.path}: not found"}


# ---------------------------------------------------------------------------
# components — declared status + the evidence that must still hold for it
# ---------------------------------------------------------------------------

SANDBOX_GUARD = E("duffel_http.py", r'startswith\("duffel_test_"\)',
                  "app path rejects any non-sandbox Duffel token")

COMPONENTS = [
    # --- Booking -----------------------------------------------------------
    dict(stage="Booking", name="Flight search & offer view", status=BU,
         evidence=[E("app.py", r"^def search\("), E("app.py", r"^def offer_view\(")],
         tests=r"\bsearch\(|/search|offer_view",
         note="No test calls the search route or offer_view."),
    dict(stage="Booking", name="Search-time eligibility ranking", status=BT,
         evidence=[E("app.py", r"^def _offer_rank_price\(")],
         tests=r"_offer_rank_price"),
    dict(stage="Booking", name="Travel policy gates (block / approve / advise)", status=BT,
         evidence=[E("policy.py", r"^def evaluate\("), E("app.py", r"policy\.evaluate\(")],
         tests=r"test_policy_"),
    dict(stage="Booking", name="Purchase: authorize → Duffel order → capture", status=BT, core=True,
         evidence=[E("app.py", r"^def book\(\)"), E("billing.py", r"^def authorize_fare\("),
                   E("billing.py", r"^def capture_authorization\(")],
         tests=r"test_book_|test_duffel_(failure|success)|test_retried_book|test_capture_",
         note="Stripe and Duffel are mocked in these tests."),
    dict(stage="Booking", name="Card on file (Stripe SetupIntent)", status=BU,
         evidence=[E("billing.py", r"^def create_setup_intent\("),
                   E("billing.py", r"^def save_payment_method\(")],
         tests=r"create_setup_intent|billing\.save_payment_method|ensure_customer",
         note="Tests seed a fake card straight into the DB; the Stripe flow itself is unexercised."),
    dict(stage="Booking", name="Travelers & cost centers", status=BT,
         evidence=[E("db.py", r"^def traveler_save\("), E("migrations/018_cost_centers.sql", r"CREATE TABLE")],
         tests=r"cost_center|traveler_"),
    dict(stage="Booking", name="Live (non-sandbox) Duffel ticketing", status=NS,
         evidence=[SANDBOX_GUARD, E("duffel.py", r'startswith\("duffel_test_"\)'),
                   E("duffel_reshop_test.py", r'startswith\("duffel_test_"\)')],
         tests=None, note="Three independent guards reject live tokens; pilot bookings need this lifted."),
    dict(stage="Booking", name="Automatic capture-failure resolution", status=NS,
         evidence=[E("docs/architecture.md", r"Nothing resolves a failed capture automatically")],
         tests=None, note="Failures are flagged (tested); clearing them is a manual DB write."),

    # --- Fare Monitoring -----------------------------------------------------
    dict(stage="Fare Monitoring", name="Monitoring toggle + manual reshop cycle", status=BU, core=True,
         evidence=[E("app.py", r"^def toggle_monitor\("), E("app.py", r"^def _run_cycle\("),
                   E("app.py", r'"/orders/<order_id>/cycle"')],
         tests=r"_run_cycle|toggle_monitor|/cycle|/monitor",
         note="Runs only when someone clicks; no test hits these routes."),
    dict(stage="Fare Monitoring", name="Simulated price source", status=BT,
         evidence=[E("prices.py", r"^class SimulatedPriceSource\b")],
         tests=r"SimulatedPriceSource|simulated"),
    dict(stage="Fare Monitoring", name="Live Duffel price source", status=BU,
         evidence=[E("prices.py", r"^class DuffelPriceSource\b")],
         tests=r"DuffelPriceSource\(",
         note="Only ever exercised against sandbox; no test instantiates it."),
    dict(stage="Fare Monitoring", name="Scheduled / automatic monitoring", status=NS,
         evidence=[E("vercel.json", r'"crons"', absent=True),
                   E("CLAUDE.md", r"There is no\s+scheduler")],
         tests=None),
    dict(stage="Fare Monitoring", name="Live-economics observation harness", status=BU,
         evidence=[E("validation/observe.py", r"OBSERVE_ONLY"), E("validation/report.py", r"change_total"),
                   E("migrations/029_reshop_observations.sql", r"reshop_observations")],
         tests=r"observe",
         note="Its 2 tests are a static source scan (safety interlock), not behaviour; "
              "never run against live orders."),

    # --- Reshop Evaluation ---------------------------------------------------
    dict(stage="Reshop Evaluation", name="engine.evaluate() gate sequence", status=BT, core=True,
         evidence=[E("engine.py", r"^def evaluate\(")],
         # test_engine.py drives evaluate() through its run(order, source) helper
         tests=r"\bevaluate\(|(?<![\w.])run\(|fee_split"),
    dict(stage="Reshop Evaluation", name="Identical-itinerary matching (carrier + flight numbers)", status=BT,
         evidence=[E("prices.py", r"^    def matches\(self, other\)")],
         tests=r"identical|itinerary|codeshare|connection|segment", names_only=True),
    dict(stage="Reshop Evaluation", name="Fare eligibility (available_actions over conditions)", status=BT,
         evidence=[E("eligibility.py", r"^def assess\(")],
         tests=r"\bassess\(|eligib|CUSTOMER_COPY|from_duffel|available_actions"),
    dict(stage="Reshop Evaluation", name="Carrier change-capability record", status=BT,
         evidence=[E("db.py", r"^def carrier_capability_record\("),
                   E("migrations/027_carrier_change_capability.sql", r"carrier_change_capability")],
         tests=r"carrier_capability"),
    dict(stage="Reshop Evaluation", name="Decision audit trail (append-only)", status=BT,
         evidence=[E("engine.py", r"^def log_decision\("), E("db.py", r"^def audit_append\(")],
         tests=r"log_decision|log_execution|audit"),

    # --- Gate ------------------------------------------------------------------
    dict(stage="Gate", name="change_total_amount < 0 + min_saving floor", status=BT, core=True,
         evidence=[E("engine.py", r"if best\.change_total >= 0:"),
                   E("engine.py", r"if saving < policy\.min_saving:")],
         # names only: nearly every engine test body passes change_total to its fake source
         tests=r"floor|change_total|market|fractional_cent|declined_copy", names_only=True),
    dict(stage="Gate", name="Gate validated on live fares", status=NS, core=True,
         evidence=[E("docs/architecture.md", r"always exactly\s+`\+125\.00`"), SANDBOX_GUARD],
         tests=None,
         note="Sandbox hardcodes change_total_amount = +125.00, so this gate has never passed on real data."),

    # --- CONFIRM ---------------------------------------------------------------
    dict(stage="CONFIRM", name="Two-step typed CONFIRM", status=BT, core=True,
         evidence=[E("app.py", r"^def confirm_action\("), E("app.py", r'!= "CONFIRM"')],
         tests=r"confirm_text|confirm_action"),
    dict(stage="CONFIRM", name="Role-based permission to confirm", status=ST,
         evidence=[E("auth.py", r"^def role_required\("),
                   E("*.py", r"@auth\.role_required|@role_required", absent=True)],
         tests=r"role_required",
         note="Defined, never applied: any user in an account can execute."),
    dict(stage="CONFIRM", name="Algorithmic (unattended) execution", status=NS,
         evidence=[E("CLAUDE.md", r"Nothing executes autonomously")],
         tests=None),

    # --- Execution ---------------------------------------------------------------
    dict(stage="Execution", name="Exchange via Duffel Order Change", status=BT, core=True,
         evidence=[E("app.py", r'"/air/order_changes"')],
         tests=r"order_changes|/execute/exchange|exchange_negative",
         note="Duffel mocked; the negative-delta branch has never run live."),
    dict(stage="Execution", name="Cancel + refund classification (card / credit / forfeited)", status=BT, core=True,
         evidence=[E("app.py", r'"/air/order_cancellations"')],
         tests=r"order_cancellations|/execute/cancel|second_cancel|forfeited|refund_to_card|airline_credit"),
    dict(stage="Execution", name="Execution idempotency guard", status=BT, core=True,
         evidence=[E("db.py", r"^def claim_execution\(")],
         tests=r"claim_execution|second_cancel|AlreadyAttempted|exactly_once"),
    dict(stage="Execution", name="Void-and-rebook", status=NS,
         evidence=[E("engine.py", r"IN_VOID_WINDOW"),
                   E("docs/architecture.md", r"Void-and-rebook is respected but unimplemented")],
         tests=None),
    dict(stage="Execution", name="Book-new-before-cancel-old (manual reservations)", status=NS,
         evidence=[E("app.py", r"book-new-before-cancel-old")],
         tests=None, note="Manual/imported reservations are watch-only."),

    # --- Fee Capture ---------------------------------------------------------------
    dict(stage="Fee Capture", name="Savings events + commission at account rate", status=BT, core=True,
         evidence=[E("db.py", r"^def savings_event_create\("), E("db.py", r"^def account_commission_rate\(")],
         tests=r"commission|savings_event|delivery_type"),
    dict(stage="Fee Capture", name="Invoice generation (ops-triggered)", status=BT, core=True,
         evidence=[E("db.py", r"^def generate_invoice\("), E("app.py", r'"/accounts/invoice/generate"')],
         tests=r"generate_invoice|invoice_lines_for|invoice_line"),
    dict(stage="Fee Capture", name="Invoice review / approval screen", status=NS,
         evidence=[E("app.py", r"Phase 4 builds the real screen")],
         tests=None),
    dict(stage="Fee Capture", name="Collecting invoice payment (Stripe)", status=NS, core=True,
         evidence=[E("*.py", r"Invoice\.(create|pay|finalize_invoice)|invoice.{0,40}PaymentIntent", absent=True),
                   E("db.py", r"not yet charged")],
         tests=None, note="Commission and subscription are computed and invoiced, never charged."),

    # --- Platform (cross-cutting, not a pipeline node) ----------------------------
    dict(stage="Platform", name="Email/password auth, sessions, throttle", status=BU,
         evidence=[E("auth.py", r"^def login_required\("), E("db.py", r"^def record_failure\(")],
         tests=r"check_password|throttled|login_required|record_failure|/login",
         note="No email verification or password reset (README)."),
    dict(stage="Platform", name="Google SSO via Supabase Auth", status=BU,
         evidence=[E("supabase_auth.py", r"not verified against a live\s+exchange")],
         tests=r"supabase_auth|exchange_code"),
    dict(stage="Platform", name="RLS lockdown on every table", status=BU,
         evidence=[E("migrations/014_lockdown_public_grants.sql", r"ROW LEVEL SECURITY"),
                   E("docs/architecture.md", r"nothing tests that a new one did")],
         tests=r"relrowsecurity|ROW LEVEL SECURITY"),
    dict(stage="Platform", name="Schema-drift guards", status=BT,
         evidence=[E("db.py", r"_ORDER_COLS")],
         tests=r"covers_every_writable_column|round_trips_every_writable"),
    dict(stage="Platform", name="Dashboard money charts (difference band, spend)", status=BT,
         evidence=[E("app.py", r"^def difference_band\("), E("db.py", r"^def spend_by_carrier\(")],
         tests=r"difference_band|execution_steps|spend_by_carrier|activity_label",
         note="db.monthly_series() (Trips chart) is untested and still sums orders.refunded."),
    dict(stage="Platform", name="Email-forward import (dormant)", status=BU,
         evidence=[E("parsing.py", r"."), E("app.py", r'"/webhooks/resend/inbound"')],
         tests=r"\bparsing\.|webhooks/resend",
         note="Routed but not to be extended (CLAUDE.md)."),
]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_functions():
    """{(file, name): source} for every test_* function, via ast."""
    out = {}
    for f in tracked("*test_*.py"):
        if not Path(f).name.startswith("test_"):
            continue
        src = read(f) or ""
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                out[(f, node.name)] = ast.get_source_segment(src, node) or ""
    return out


def run_pytest():
    venv_py = ROOT / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    tmp = Path(tempfile.mkdtemp()) / "pytest.xml"
    print(f"running pytest with {py} (real DB; a few minutes)…", file=sys.stderr)
    subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={tmp}"],
                   cwd=ROOT, timeout=3600)
    return tmp if tmp.exists() else None


def load_junit(path):
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    results = {}
    for tc in suite.iter("testcase"):
        parts = (tc.get("classname") or "").split(".")
        name = (tc.get("name") or "").split("[")[0]
        # classname is module path, possibly followed by a class name
        file = None
        for i in range(len(parts), 0, -1):
            candidate = "/".join(parts[:i]) + ".py"
            if (ROOT / candidate).exists():
                file = candidate
                break
        outcome = "passed"
        if tc.find("failure") is not None:
            outcome = "failed"
        elif tc.find("error") is not None:
            outcome = "error"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        results.setdefault((file, name), []).append(outcome)
    totals = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    totals["time"] = float(suite.get("time", 0) or 0)
    totals["timestamp"] = suite.get("timestamp", "")
    return results, totals


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def resolve_components(tests, results):
    matched_any = set()
    for c in COMPONENTS:
        c["checks"] = [e.run() for e in c["evidence"]]
        missing = [k for k in c["checks"] if not k["ok"]]
        c["declared"] = c["status"]
        c["resolved"] = UN if missing else c["status"]
        c["unknown_why"] = "; ".join(k["cite"] for k in missing)
        c.setdefault("core", False)
        c.setdefault("note", "")

        c["test_names"], c["runs"] = [], {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        if c["tests"]:
            rx = re.compile(c["tests"], re.S)
            for (f, name), body in sorted(tests.items()):
                if rx.search(name) or (not c.get("names_only") and rx.search(body)):
                    c["test_names"].append(f"{f}::{name}")
                    matched_any.add((f, name))
                    for outcome in results.get((f, name), []):
                        c["runs"][outcome] += 1
        failing = c["runs"]["failed"] + c["runs"]["error"]
        if c["resolved"] == BT and not c["test_names"]:
            c["resolved"], c["unknown_why"] = UN, "declared tested, but no matching test found"
        c["failing"] = failing
    unmapped = sorted(f"{f}::{n}" for (f, n) in tests if (f, n) not in matched_any)
    return unmapped


def stage_summary(stage):
    comps = [c for c in COMPONENTS if c["stage"] == stage]
    known = [c for c in comps if c["resolved"] != UN]
    pct = round(100 * sum(WEIGHT[c["resolved"]] for c in known) / len(known)) if known else None
    core = [c for c in comps if c["core"]] or comps
    node = max((c["resolved"] for c in core), key=RANK.get)
    gaps = [c for c in comps if not c["core"] and c["resolved"] in (ST, NS, UN)]
    return {"stage": stage, "components": comps, "pct": pct, "node_status": node,
            "core": core, "gaps": gaps, "unknown": len(comps) - len(known),
            "counts": {s: sum(1 for c in comps if c["resolved"] == s) for s in STATUSES}}


def git_state():
    head = git("rev-parse", "--short", "HEAD").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    lr = git("rev-list", "--left-right", "--count", "origin/main...HEAD").split()
    behind, ahead = (int(lr[0]), int(lr[1])) if len(lr) == 2 else (None, None)
    dirty = [l[3:] for l in git("status", "--porcelain", "--untracked-files=all").splitlines()
             if l[3:].strip('"') not in SELF]
    staging = git("log", "-1", "--date=short", "--pretty=%h %ad", "origin/staging").strip()
    main_date = git("log", "-1", "--date=short", "--pretty=%ad", "origin/main").strip()
    last30 = []
    for line in git("log", "-30", "--date=short", "--pretty=%h%x09%ad%x09%an%x09%s").splitlines():
        h, d, a, s = (line.split("\t", 3) + ["", "", "", ""])[:4]
        last30.append({"hash": h, "date": d, "author": a, "subject": s})
    today = dt.date.today()
    start = today - dt.timedelta(days=29)
    days = {(start + dt.timedelta(days=i)).isoformat(): 0 for i in range(30)}
    authors = {}
    for line in git("log", f"--since={start.isoformat()} 00:00", "--date=short", "--pretty=%ad%x09%an").splitlines():
        d, _, a = line.partition("\t")
        if d in days:
            days[d] += 1
            authors[a] = authors.get(a, 0) + 1
    return {"head": head, "branch": branch, "ahead": ahead, "behind": behind, "dirty": dirty,
            "staging": staging, "main_date": main_date, "last30": last30,
            "days": days, "authors": authors}


def inventory():
    def loc(f):
        return (read(f) or "").count("\n")
    py = [f for f in tracked("*.py") if not Path(f).name.startswith("test_")]
    todo_rx = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
    todos = []
    for f in tracked("*"):
        if f.endswith((".png", ".jpg", ".ico")):
            continue
        for i, line in enumerate((read(f) or "").splitlines(), 1):
            if todo_rx.search(line):
                todos.append(f"{f}:{i}: {line.strip()[:120]}")
    return {
        "modules": sorted(((f, loc(f)) for f in py), key=lambda x: -x[1]),
        "docs": [(f, loc(f)) for f in tracked("*.md")] + [(f, loc(f)) for f in tracked("docs/*") if not f.endswith(".md")],
        "configs": [f for f in tracked("*") if re.search(r"(^|/)(vercel\.json|requirements[^/]*\.txt|\.env\.example|\.vercelignore)$", f)],
        "migrations": tracked("migrations/*.sql"),
        "templates": tracked("templates/*"),
        "todos": todos,
    }


def floor_value():
    m = re.search(r'DEFAULT_POLICY = ReshopPolicy\(min_saving=Decimal\("([\d.]+)"\)', read("app.py") or "")
    e = re.search(r'min_saving: Decimal = Decimal\("([\d.]+)"\)', read("engine.py") or "")
    return (m.group(1) if m else None), (e.group(1) if e else None)


def first_line(path, pattern):
    text = read(path) or ""
    m = re.search(pattern, text, re.M)
    return f"{path}:{line_of(text, m.start())}" if m else None


def context_checks(inv, gs):
    """CONTEXT claims supplied with the request, each checked against the repo."""
    def found_any(pattern, glob="*"):
        rx = re.compile(pattern, re.I)
        return [f for f in tracked(glob) if not f.endswith((".png", ".jpg")) and rx.search(read(f) or "")]

    app_floor, engine_floor = floor_value()
    confirm_at = first_line("app.py", r'!= "CONFIRM"')
    gate_at = first_line("engine.py", r"if best\.change_total >= 0:")
    guard_at = first_line("duffel_http.py", r'startswith\("duffel_test_"\)')
    balance_at = [x for x in (first_line("app.py", r'"payments": \[\{"type": "balance"'),
                              first_line("app.py", r'"payment": \{"type": "balance"')) if x]
    sub_at = first_line("db.py", r"subscription_fee")
    credit_at = first_line("db.py", r"delivery_type IN \('refund_to_card', 'airline_credit'\)")
    rate_at = first_line("migrations/015_accounts_company_fields.sql", r"commission_rate numeric")
    authors = sorted(gs["authors"]) or ["(none in window)"]
    connect_hits = found_any(r"Stripe Connect")
    connect_text = ("'Stripe Connect' appears nowhere." if not connect_hits
                    else f"'Stripe Connect' appears in {', '.join(connect_hits)}.")

    return [
        dict(claim="B2B/enterprise flight booking + post-booking repricing",
             verdict="Consistent", evidence=f"{first_line('CLAUDE.md', 'corporate travel manager')}; engine.py"),
        dict(claim="Delaware C corp; co-founders Phoenix, Jakeb",
             verdict="Unknown" if not found_any(r"Delaware|C[- ]corp|Jakeb") else "Consistent",
             evidence="No repo mention of Delaware, C corp, or Jakeb. Git authors in the last 30 days: "
                      + ", ".join(authors) + "."),
        dict(claim="Beta, phase one",
             verdict="Unknown", evidence="No 'beta' in the repo. 'Phase 1/2/4' in code are build phases "
                                         "(e.g. app.py 'Phase 4 builds the real screen'), not a company stage."),
        dict(claim="Fares via Duffel",
             verdict="Qualified", evidence=f"Sandbox only, by construction: {guard_at} rejects any non-test token."),
        dict(claim="Reshop engine built and unit-tested",
             verdict="Consistent",
             evidence=f"engine.py; {len({t for c in COMPONENTS if c['stage'] in ('Reshop Evaluation', 'Gate') for t in c['test_names']})} "
                      "distinct tests exercise evaluation and gate components."),
        dict(claim="Execution human-gated behind CONFIRM",
             verdict="Qualified", evidence=f"True, but the gate is in app.py:execute ({confirm_at}), not engine.py; "
                                           "engine.py only marks a reshop AWAITING_CONFIRMATION. Roles are not enforced, "
                                           "so any user in the account can confirm."),
        dict(claim="Decision relies on change_total_amount < 0; sandbox hardcodes +125.00",
             verdict="Consistent", evidence=f"{gate_at} skips on >= 0; docs/architecture.md documents +125.00. "
                                            f"A second gate also applies: saving must clear min_saving (${app_floor})."),
        dict(claim="Revenue: 25% success fee, only on realized savings",
             verdict="Contradicted",
             evidence=f"Rate is per account (default 0.25, {rate_at}); invoices add a flat subscription_fee line "
                      f"({sub_at}); commission is also billed on airline credits, not only cash ({credit_at})."),
        dict(claim="Open decision: fare-drop floor $40–50 vs Autopilot's $20",
             verdict="Code at $20",
             evidence=f"app.py DEFAULT_POLICY min_saving = ${app_floor}; engine.py default ${engine_floor}. "
                      "Code currently matches Autopilot's floor."),
        dict(claim="Remove Duffel Balance capital + Stripe Connect split from strategy docs",
             verdict="Partly contradicted",
             evidence=f"No strategy docs are in the repo. {connect_text} But Duffel Balance is how the "
                      f"code pays for tickets and exchange top-ups ({', '.join(balance_at)}); "
                      "dropping it from strategy would leave docs disagreeing with code."),
        dict(claim="Pilot accounts pending from Jakeb",
             verdict="Unknown", evidence="No repo evidence. eligibility.py only notes a threshold is "
                                         "'tunable against pilot data'."),
        dict(claim="Jakeb wants algorithmic (not human-queue) repricing",
             verdict="Conflicts with current design",
             evidence=f"{first_line('CLAUDE.md', 'Nothing executes autonomously')} 'Nothing executes autonomously'; "
                      "no crons in vercel.json; every cycle and execution is a click."),
    ]


def risks(totals, gs, stages):
    guards = [x for x in (first_line("duffel_http.py", r'startswith\("duffel_test_"\)'),
                          first_line("duffel.py", r'startswith\("duffel_test_"\)'),
                          first_line("duffel_reshop_test.py", r'startswith\("duffel_test_"\)')) if x]
    out = [
        dict(sev="critical", title="Unit economics are unvalidated",
             body="The whole decision rests on Duffel returning change_total_amount < 0. The sandbox hardcodes "
                  "+125.00 for every fare, carrier and date, so the gate has never passed on real data and "
                  "the success fee has never been earned outside a mock.",
             cite=[first_line("engine.py", r"if best\.change_total >= 0:"),
                   first_line("docs/architecture.md", r"always exactly")]),
        dict(sev="critical", title="Duffel is sandbox-only by construction",
             body="Three independent guards reject any live token. A pilot cannot issue a real ticket, and "
                  "validation/observe.py cannot see a real quote, until the app-path guard is deliberately lifted.",
             cite=guards),
        dict(sev="serious", title="Fees are invoiced but never collected",
             body="savings_events and invoices are built and tested, but nothing charges an invoice. Generation is "
                  "a manual ops POST that returns JSON; there is no review screen and no Stripe collection.",
             cite=[first_line("db.py", r"not yet charged"), first_line("app.py", r"Phase 4 builds the real screen")]),
        dict(sev="serious", title="No scheduler; the algorithmic repricing ask conflicts with the design",
             body="Monitoring cycles run only when someone clicks, and every exchange needs a typed CONFIRM. "
                  "Unattended repricing needs a cron, a state model, and a decision to relax the human gate.",
             cite=[first_line("CLAUDE.md", r"Nothing executes autonomously"),
                   first_line("docs/architecture.md", r"Revisit when the scheduler lands")]),
        dict(sev="serious", title="Roles are stored but not enforced",
             body="users.role exists (admin | booker | approver | finance) but auth.role_required is never applied. "
                  "Any user in a pilot company can book, cancel or exchange. Orders are correctly scoped to "
                  "the user's account.",
             cite=[first_line("auth.py", r"^def role_required\("), first_line("app.py", r"^def find_order\(")]),
        dict(sev="warning", title="Tests run against the live database",
             body="The money-path suite uses POSTGRES_URL (the production DB), writes global carrier reference data "
                  "unmocked, and grows the append-only audit_events table on every run.",
             cite=[first_line("README.md", r"real DB"), first_line("docs/architecture.md", r"Tests can\s+still corrupt")]),
    ]
    if totals and (totals["failures"] or totals["errors"]):
        out.insert(0, dict(sev="critical", title="Test suite is failing",
                           body=f"{totals['failures']} failed, {totals['errors']} errors of {totals['tests']}.", cite=[]))
    if gs["ahead"]:
        out.append(dict(sev="warning", title=f"{gs['ahead']} built commits are not deployed",
                        body=f"Local {gs['branch']} is {gs['ahead']} commits ahead of origin/main, which production "
                             "deploys from, so they are not live." +
                             (f" Uncommitted: {', '.join(gs['dirty'])}." if gs["dirty"] else ""),
                        cite=[c["hash"] + " " + c["subject"][:60] for c in gs["last30"][:gs["ahead"]]]))
    if gs["staging"] and gs["main_date"] and gs["staging"].split()[-1] < gs["main_date"]:
        out.append(dict(sev="warning", title="Staging branch is stale",
                        body=f"README says staging tracks the staging branch; origin/staging is at {gs['staging']}.",
                        cite=[first_line("README.md", r"staging tracks")]))
    if first_line("docs/architecture.md", r"Commission-rate drift") and not any(
            re.search(r"25 ?%", read(f) or "") for f in tracked("templates/*")):
        out.append(dict(sev="warning", title="Architecture doc lists a gap that is already fixed",
                        body="docs/architecture.md still reports hardcoded 25% in templates; no template contains it now.",
                        cite=[first_line("docs/architecture.md", r"Commission-rate drift")]))
    if re.search(r"sum\(o\.refunded\)", read("db.py") or ""):
        out.append(dict(sev="warning", title="Trips chart aggregates orders.refunded",
                        body="db.monthly_series() sums orders.refunded across orders — the pattern CLAUDE.md says "
                             "never to use; savings_events is canonical.",
                        cite=[first_line("db.py", r"sum\(o\.refunded\)")]))
    if first_line("README.md", r"no email verification"):
        out.append(dict(sev="warning", title="No email verification or password reset",
                        body="Anyone can sign up as any address. Needs an email-provider decision before pilot users.",
                        cite=[first_line("README.md", r"no email verification")]))
    return out


DECISIONS = [
    dict(title="Fare-drop floor",
         options="$40–50 vs Autopilot's $20",
         code=lambda: "Code today: app.py DEFAULT_POLICY min_saving = ${}.".format(floor_value()[0]),
         unblocks="Floor in ReshopPolicy; what counts as a win in pilot reporting"),
    dict(title="Strategic path",
         options="Gap attack · genuine B2B · reconsideration",
         code=lambda: "Code today: built as a B2B corporate portal (accounts, roles, cost centers, policy, invoices). "
                      "No repo evidence for the other two paths.",
         unblocks="Pilot scope, onboarding, pricing"),
    dict(title="Duffel Balance capital + Stripe Connect split",
         options="Remove both from strategy docs",
         code=lambda: "Code today: bookings and exchange top-ups are paid from TD's Duffel Balance; "
                      "Stripe Connect is not referenced. No strategy docs are in this repo.",
         unblocks="Working-capital plan for live ticketing"),
    dict(title="Repricing mode",
         options="Algorithmic (Jakeb) vs human CONFIRM queue",
         code=lambda: "Code today: human-gated. Every cycle is a click; every execution needs a typed CONFIRM.",
         unblocks="Scheduler, order state model, role enforcement"),
    dict(title="Revenue model as implemented",
         options="25% of cash savings only vs code's rate + subscription + credit commission",
         code=lambda: "Code today: per-account commission_rate (default 0.25) on cash and airline credit, "
                      "plus an optional subscription_fee line.",
         unblocks="Invoice collection build, pilot contract terms"),
]

MILESTONES = [
    dict(id="M1", title="Settle the floor, repricing mode and revenue model",
         why="Three open decisions that the later builds depend on.", needs=[]),
    dict(id="M2", title="Ship what's built: push local commits, reconcile staging",
         why="Commission-copy fixes and observe.py are not in production.", needs=[]),
    dict(id="M3", title="Separate the test database from production",
         why="Must happen before live bookings exist; tests write real rows today.", needs=[]),
    dict(id="M4", title="Get live Duffel credentials; lift the app-path sandbox guard deliberately",
         why="duffel_http.py rejects live tokens. Leave the two spike guards in place.", needs=["M3"]),
    dict(id="M5", title="Run observe.py on real orders; read report.py",
         why="Go/no-go: how often change_total_amount < 0 actually occurs, by carrier and lead time.",
         needs=["M4"]),
    dict(id="M6", title="Enforce roles on booking and execution",
         why="Pilot companies have several users; any of them can execute today.", needs=[]),
    dict(id="M7", title="Tune the floor against observed quotes",
         why="Set ReshopPolicy.min_saving from M1's decision and M5's data.", needs=["M1", "M5"]),
    dict(id="M8", title="Collect fees: invoice review + Stripe live-mode charging",
         why="Revenue is computed but never charged.", needs=["M1"]),
    dict(id="M9", title="Scheduled monitoring cycles (and unattended execution, if chosen)",
         why="Cron + order state model; unattended execution also needs roles and a tuned floor.",
         needs=["M1", "M5", "M6", "M7"]),
    dict(id="M10", title="Onboard pilot accounts",
         why="Needs live ticketing, validated economics, roles and fee collection.",
         needs=["M2", "M5", "M6", "M8", "M9"]),
]


def topo(milestones):
    by_id = {m["id"]: m for m in milestones}
    done, order = set(), []
    while len(order) < len(milestones):
        ready = [m for m in milestones if m["id"] not in done and all(n in done for n in m["needs"])]
        if not ready:
            raise ValueError("milestone dependency cycle")
        for m in ready:
            m["depth"] = 1 + max((by_id[n]["depth"] for n in m["needs"]), default=0)
        for m in ready:
            done.add(m["id"])
            order.append(m)
    return order


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def chip(status):
    return (f'<span class="chip s-{STATUS_KEY[status]}"><span class="dot" aria-hidden="true">'
            f'{STATUS_ICON[status]}</span>{esc(status)}</span>')


def meter(pct):
    if pct is None:
        return '<div class="meter-label">Unknown</div>'
    return (f'<div class="meter" role="img" aria-label="{pct}% complete"><span style="width:{pct}%"></span></div>'
            f'<div class="meter-label">{pct}% complete</div>')


def render_pipeline(summaries):
    parts = []
    for i, s in enumerate(summaries):
        core = "".join(f'<li>{chip(c["resolved"])}<span>{esc(c["name"])}</span></li>' for c in s["core"])
        gaps = ""
        if s["gaps"]:
            gaps = ('<div class="gaps"><b>Gaps</b><ul>' +
                    "".join(f'<li>{STATUS_ICON[g["resolved"]]} {esc(g["name"])}</li>' for g in s["gaps"]) +
                    "</ul></div>")
        warn = ""
        if s["stage"] == "Gate":
            warn = '<p class="node-warn">⚠ Never passed on real data: sandbox returns +125.00</p>'
        parts.append(
            f'<article class="node s-{STATUS_KEY[s["node_status"]]}" aria-label="{esc(s["stage"])}: {esc(s["node_status"])}">'
            f'<header><span class="step">{i + 1}</span><h3>{esc(GATE_LABEL.get(s["stage"], s["stage"]))}</h3></header>'
            f'<div class="node-status"><div class="kicker">Weakest core</div>{chip(s["node_status"])}</div>{meter(s["pct"])}'
            f'<ul class="core">{core}</ul>{warn}{gaps}</article>')
        if i < len(summaries) - 1:
            parts.append('<div class="arrow" aria-hidden="true">→</div>')
    return "".join(parts)


def render_components(stage_order, summaries_by_stage, have_results):
    rows = []
    for stage in stage_order:
        s = summaries_by_stage[stage]
        pct = f'{s["pct"]}%' if s["pct"] is not None else "Unknown"
        rows.append(f'<tr class="group"><th colspan="4">{esc(stage)} <span class="muted">· {pct} complete</span></th></tr>')
        for c in s["components"]:
            cites = "".join(
                f'<li class="{"" if k["ok"] else "bad"}"><code>{esc(k["cite"])}</code>'
                f'{(" — " + esc(k["note"])) if k["note"] else ""}</li>' for k in c["checks"])
            n = len(c["test_names"])
            cases = sum(c["runs"].values())
            if n and have_results:
                r = c["runs"]
                res = f'{r["passed"]} passed' + (f', <b class="fail">{c["failing"]} failing</b>' if c["failing"] else "") \
                      + (f', {r["skipped"]} skipped' if r["skipped"] else "")
                if cases != n:
                    res += f' <span class="muted">({n} functions, parametrized)</span>'
                n = cases
            elif n:
                res = "results Unknown"
            else:
                res = "—"
            test_list = ""
            if n:
                test_list = ('<details><summary>matched tests</summary><ul class="tests">' +
                             "".join(f"<li><code>{esc(t)}</code></li>" for t in c["test_names"]) + "</ul></details>")
            why = f'<div class="why">Unknown because: {esc(c["unknown_why"])}</div>' if c["resolved"] == UN else ""
            note = f'<div class="note">{esc(c["note"])}</div>' if c["note"] else ""
            core = ' <span class="tag">core</span>' if c["core"] else ""
            rows.append(
                f'<tr data-status="{STATUS_KEY[c["resolved"]]}"><td>{chip(c["resolved"])}</td>'
                f'<td><b>{esc(c["name"])}</b>{core}{note}{why}</td>'
                f'<td><ul class="cites">{cites}</ul></td>'
                f'<td class="num"><b>{n}</b><div class="muted small">{res}</div>{test_list}</td></tr>')
    return "".join(rows)


def render_chart(days):
    items = list(days.items())
    peak = max([v for _, v in items] + [1])
    ticks = 4
    step = max(1, -(-peak // ticks))
    top = step * ticks
    # Drawn near 1:1 with the desktop content width so tick text stays ~11px;
    # narrow screens scroll the chart inside .chart-scroll instead of shrinking it.
    W, H, L, R, T, B = 1180, 240, 34, 8, 20, 34
    pw, ph = W - L - R, H - T - B
    band = pw / len(items)
    bw = min(24, band - 2)
    svg = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-labelledby="commits-title">']
    for i in range(ticks + 1):
        y = T + ph - ph * i / ticks
        svg.append(f'<line class="grid{" base" if i == 0 else ""}" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        svg.append(f'<text class="tick" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{step * i}</text>')
    peak_i = max(range(len(items)), key=lambda i: items[i][1])
    for i, (d, v) in enumerate(items):
        x = L + band * i + (band - bw) / 2
        date = dt.date.fromisoformat(d)
        label = f"{date:%b} {date.day}"
        if v:
            h = ph * v / top
            y = T + ph - h
            r = min(4, h / 2, bw / 2)
            path = (f"M{x:.1f},{T + ph:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                    f"H{x + bw - r:.1f} Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{T + ph:.1f} Z")
            svg.append(f'<path class="bar" d="{path}"/>')
            if i == peak_i:
                svg.append(f'<text class="val" x="{x + bw / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">{v}</text>')
        svg.append(f'<rect class="hit" x="{L + band * i:.1f}" y="{T}" width="{band:.1f}" height="{ph}" '
                   f'data-tip="{esc(label)}: {v} commit{"s" if v != 1 else ""}" tabindex="0"/>')
        if i % 5 == 0 or i == len(items) - 1:
            svg.append(f'<text class="tick" x="{L + band * i + band / 2:.1f}" y="{H - 12}" text-anchor="middle">{esc(label)}</text>')
    svg.append("</svg>")
    table = "".join(f"<tr><td>{esc(d)}</td><td class='num'>{v}</td></tr>" for d, v in items if v)
    return "".join(svg), table


CSS = """
:root{
  color-scheme:light;
  --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#6f6e69;
  --grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);
  --series:#2a78d6;--track:#cde2fb;
  --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--unknown:#898781;
  --tint-good:rgba(12,163,12,.08);--tint-warning:rgba(250,178,25,.12);--tint-serious:rgba(236,131,90,.12);
  --tint-critical:rgba(208,59,59,.08);--tint-unknown:rgba(137,135,129,.10);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink-2:#c3c2b7;--muted:#9a988f;
    --grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
    --series:#3987e5;--track:#184f95;
    --tint-good:rgba(12,163,12,.07);--tint-warning:rgba(250,178,25,.07);--tint-serious:rgba(236,131,90,.08);
    --tint-critical:rgba(208,59,59,.10);--tint-unknown:rgba(137,135,129,.10);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink-2:#c3c2b7;--muted:#9a988f;
  --grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
  --series:#3987e5;--track:#184f95;
  --tint-good:rgba(12,163,12,.07);--tint-warning:rgba(250,178,25,.07);--tint-serious:rgba(236,131,90,.08);
  --tint-critical:rgba(208,59,59,.10);--tint-unknown:rgba(137,135,129,.10);
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding-inline:16px;padding-block:28px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:0 0 12px}
h3{font-size:14px;margin:0;line-height:1.3}
p{margin:0 0 8px}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-2)}
.muted{color:var(--muted)}.small{font-size:12px}
.sub{color:var(--ink-2);margin-bottom:20px}
section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:18px;margin-top:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 14px}
.tile .label{color:var(--ink-2);font-size:12px}
.tile .value{font-size:24px;font-weight:600;margin-top:2px}
.tile .detail{color:var(--muted);font-size:12px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;white-space:nowrap;color:var(--ink)}
.chip .dot{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;font-size:11px;color:#0b0b0b;background:var(--c)}
.s-good{--c:var(--good);--tint:var(--tint-good)}.s-warning{--c:var(--warning);--tint:var(--tint-warning)}
.s-serious{--c:var(--serious);--tint:var(--tint-serious)}.s-critical{--c:var(--critical);--tint:var(--tint-critical)}
.s-unknown{--c:var(--unknown);--tint:var(--tint-unknown)}
.s-critical .dot,.s-unknown .dot{color:#fff}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 12px}
.scroll{overflow-x:auto;padding-bottom:6px}
.pipeline{display:flex;align-items:stretch;min-width:1076px}
.node{flex:1 1 0;min-width:140px;background:var(--surface);border:1px solid var(--ring);border-top:4px solid var(--c);border-radius:10px;padding:10px}
.node header{display:flex;gap:8px;align-items:flex-start;margin-bottom:8px}
.node h3{overflow-wrap:anywhere}
.node .kicker{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.step{flex:none;width:20px;height:20px;border-radius:50%;background:var(--surface);border:1px solid var(--ring);display:grid;place-items:center;font-size:11px;color:var(--ink-2)}
.node-status{margin-bottom:6px}
.arrow{flex:none;display:grid;place-items:center;width:16px;color:var(--muted);font-size:14px}
.meter{height:6px;border-radius:3px;background:var(--track);overflow:hidden}
.meter span{display:block;height:100%;background:var(--series);border-radius:3px}
.meter-label{font-size:11px;color:var(--ink-2);margin:3px 0 8px}
.core,.gaps ul{list-style:none;margin:0;padding:0}
.core li{display:flex;flex-direction:column;gap:2px;margin-bottom:6px;font-size:12px;color:var(--ink-2)}
.gaps{border-top:1px solid var(--ring);margin-top:6px;padding-top:6px;font-size:11.5px;color:var(--ink-2)}
.gaps li{margin-top:2px}
.node-warn{font-size:11.5px;font-weight:600;color:var(--ink);background:var(--surface);border-left:3px solid var(--critical);padding:4px 6px;border-radius:4px;margin:6px 0 0}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.filters button{font:inherit;font-size:12px;border:1px solid var(--ring);background:var(--surface);color:var(--ink);border-radius:999px;padding:4px 10px;cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--surface)}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--grid)}
thead th{font-size:12px;color:var(--ink-2);font-weight:600}
tr.group th{background:var(--page);font-size:13px;padding-top:12px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cites{list-style:none;margin:0;padding:0}.cites li.bad code{color:var(--critical)}
.note{font-size:12px;color:var(--ink-2);margin-top:2px}
.why{font-size:12px;margin-top:2px;border-left:3px solid var(--unknown);padding-left:6px}
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--ring);border-radius:4px;padding:0 4px;color:var(--ink-2);margin-left:4px}
.fail{color:var(--critical)}
details summary{cursor:pointer;color:var(--ink-2);font-size:12px}
ul.tests{margin:4px 0 0;padding-left:14px;text-align:left}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.grid2>section{margin-top:0}
.risk{border:1px solid var(--ring);border-left:4px solid var(--c);background:var(--tint);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.risk h3{display:flex;gap:8px;align-items:center;margin-bottom:4px}
.risk p{color:var(--ink-2);margin:0 0 4px}
.risk .cite code{display:inline-block;margin-right:8px}
.sev{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.decision{border-bottom:1px solid var(--grid);padding:8px 0}.decision:last-child{border-bottom:0}
.decision .opts{font-weight:600}
.decision p{color:var(--ink-2);margin:2px 0}
ol.milestones{list-style:none;margin:0;padding:0;counter-reset:m}
ol.milestones li{display:grid;grid-template-columns:48px 1fr;gap:10px;padding:8px 0;border-bottom:1px solid var(--grid)}
ol.milestones li:last-child{border-bottom:0}
.mid{font-weight:700;font-variant-numeric:tabular-nums}
.needs{font-size:12px;color:var(--muted)}
.verdict{font-weight:600;white-space:nowrap}
.chart-scroll{min-width:640px}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--grid);stroke-width:1}.chart .grid.base{stroke:var(--axis)}
.chart .tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.chart .val{fill:var(--ink);font-size:11px;font-weight:600}
.chart .bar{fill:var(--series)}
.chart .hit{fill:transparent;cursor:default;outline:none}
.chart .hit:hover,.chart .hit:focus{fill:var(--ring)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);font-size:12px;padding:4px 8px;border-radius:6px;opacity:0;transition:opacity .08s;z-index:10}
.cols{columns:2 320px;column-gap:24px}
.cols li{break-inside:avoid}
footer{color:var(--muted);font-size:12px;margin-top:20px}
"""

JS = """
(function(){
  var tip=document.getElementById('tip');
  function show(e){var t=e.target.getAttribute('data-tip');if(!t)return;tip.textContent=t;tip.style.opacity=1;move(e)}
  function move(e){var r=e.target.getBoundingClientRect();var x=e.clientX||r.left+r.width/2,y=e.clientY||r.top;
    tip.style.left=Math.min(x+12,window.innerWidth-tip.offsetWidth-8)+'px';tip.style.top=(y-34)+'px'}
  function hide(){tip.style.opacity=0}
  document.querySelectorAll('.chart .hit').forEach(function(el){
    el.addEventListener('mouseenter',show);el.addEventListener('mousemove',move);el.addEventListener('mouseleave',hide);
    el.addEventListener('focus',show);el.addEventListener('blur',hide);
  });
  var buttons=document.querySelectorAll('.filters button');
  buttons.forEach(function(b){b.addEventListener('click',function(){
    buttons.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')});
    var want=b.getAttribute('data-filter');
    document.querySelectorAll('#components tbody tr').forEach(function(tr){
      if(tr.classList.contains('group')){tr.hidden=false;return}
      tr.hidden=!(want==='all'||tr.getAttribute('data-status')===want);
    });
    document.querySelectorAll('#components tbody tr.group').forEach(function(g){
      var n=g.nextElementSibling,any=false;
      while(n&&!n.classList.contains('group')){if(!n.hidden)any=true;n=n.nextElementSibling}
      g.hidden=!any;
    });
  })});
})();
"""


def render(data):
    gs, totals, inv = data["git"], data["totals"], data["inventory"]
    summaries = data["summaries"]
    by_stage = {s["stage"]: s for s in summaries}
    pipeline_s = [by_stage[s] for s in PIPELINE]
    all_counts = {s: sum(1 for c in COMPONENTS if c["resolved"] == s) for s in STATUSES}

    known = [c for c in COMPONENTS if c["stage"] in PIPELINE and c["resolved"] != UN]
    overall = round(100 * sum(WEIGHT[c["resolved"]] for c in known) / len(known)) if known else None

    if totals:
        tests_value = f'{totals["passed"]}/{totals["tests"]}'
        tests_detail = (f'{totals["failures"]} failed · {totals["errors"]} errors · {totals["skipped"]} skipped · '
                        f'{totals["time"]:.0f}s')
    else:
        tests_value, tests_detail = "Unknown", "pytest results not supplied"
    ahead = "Unknown" if gs["ahead"] is None else str(gs["ahead"])
    commits30 = sum(gs["days"].values())
    active = sum(1 for v in gs["days"].values() if v)

    tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="label">Pipeline complete (weighted)</div><div class="value">{overall if overall is not None else 'Unknown'}{'%' if overall is not None else ''}</div><div class="detail">{len(known)} components with evidence</div></div>
      <div class="tile"><div class="label">Tests passing</div><div class="value">{tests_value}</div><div class="detail">{esc(tests_detail)}</div></div>
      <div class="tile"><div class="label">All components by status</div><div class="value">{all_counts[BT]} <span class="muted small">tested</span></div><div class="detail">{all_counts[BU]} untested · {all_counts[ST]} stubbed · {all_counts[NS]} not started · {all_counts[UN]} unknown</div></div>
      <div class="tile"><div class="label">Commits, last 30 days</div><div class="value">{commits30}</div><div class="detail">{active} active days · {esc(', '.join(f'{a} {n}' for a, n in gs['authors'].items()) or 'none')}</div></div>
      <div class="tile"><div class="label">Undeployed commits</div><div class="value">{ahead}</div><div class="detail">local {esc(gs['branch'])} vs origin/main · HEAD {esc(gs['head'])}</div></div>
    </div>"""

    legend = "".join(chip(s) for s in STATUSES)
    filters = '<button aria-pressed="true" data-filter="all">All</button>' + "".join(
        f'<button aria-pressed="false" data-filter="{STATUS_KEY[s]}">{STATUS_ICON[s]} {esc(s)} ({all_counts[s]})</button>'
        for s in STATUSES if all_counts[s])

    risk_html = "".join(
        f'<div class="risk s-{r["sev"]}"><h3><span class="sev">{esc(r["sev"])}</span>{esc(r["title"])}</h3>'
        f'<p>{esc(r["body"])}</p><div class="cite">' +
        " ".join(f"<code>{esc(c)}</code>" for c in r["cite"] if c) + "</div></div>"
        for r in data["risks"])

    decision_html = "".join(
        f'<div class="decision"><h3>{esc(d["title"])}</h3><div class="opts">{esc(d["options"])}</div>'
        f'<p>{esc(d["code"]())}</p><p class="muted small">Unblocks: {esc(d["unblocks"])}</p></div>'
        for d in DECISIONS)

    context_html = "".join(
        f'<tr><td>{esc(c["claim"])}</td><td class="verdict">{esc(c["verdict"])}</td><td>{esc(c["evidence"])}</td></tr>'
        for c in data["context"])

    ms_html = "".join(
        f'<li><span class="mid">{esc(m["id"])}</span><div><b>{esc(m["title"])}</b>'
        f'<div class="muted">{esc(m["why"])}</div>'
        f'<div class="needs">{"Needs " + ", ".join(m["needs"]) if m["needs"] else "No dependencies — can start now"}'
        f' · wave {m["depth"]}</div></div></li>'
        for m in data["milestones"])

    chart_svg, chart_table = data["chart"]

    commits_html = "".join(
        f'<li><code>{esc(c["hash"])}</code> <span class="muted">{esc(c["date"])}</span> {esc(c["subject"][:110])}</li>'
        for c in gs["last30"])
    modules_html = "".join(f"<li><code>{esc(f)}</code> <span class='muted'>{n} lines</span></li>" for f, n in inv["modules"])
    docs_html = "".join(f"<li><code>{esc(f)}</code> <span class='muted'>{n} lines</span></li>" for f, n in inv["docs"])
    tests_by_file = {}
    for (f, _n) in data["tests"]:
        tests_by_file[f] = tests_by_file.get(f, 0) + 1
    tests_html = "".join(f"<li><code>{esc(f)}</code> <span class='muted'>{n} tests</span></li>" for f, n in sorted(tests_by_file.items()))
    todos_html = ("".join(f"<li><code>{esc(t)}</code></li>" for t in inv["todos"])
                  if inv["todos"] else "<li>None found in tracked files.</li>")
    unmapped_html = ("".join(f"<li><code>{esc(t)}</code></li>" for t in data["unmapped"])
                     if data["unmapped"] else "<li>Every test maps to at least one component.</li>")

    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>TripDifference Stage</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>TripDifference — current stage</h1>
  <p class="sub">Generated {esc(generated)} from <code>{esc(gs['branch'])}@{esc(gs['head'])}</code> by
  <code>scripts/stage_report.py</code>. Every status below is backed by evidence checks re-run against the repo;
  anything whose evidence is missing is marked Unknown.</p>
  {tiles}

  <section>
    <h2>Pipeline</h2>
    <div class="legend">{legend}</div>
    <p class="muted small">Each node is colored by the weakest of its core components. Completion weights:
    Built+Tested 100%, Built+Untested 50%, Stubbed 25%, Not Started 0%; Unknown excluded.</p>
    <div class="scroll"><div class="pipeline">{render_pipeline(pipeline_s)}</div></div>
  </section>

  <section>
    <h2>Components</h2>
    <div class="filters" role="group" aria-label="Filter by status">{filters}</div>
    <div class="table-wrap"><table id="components">
      <thead><tr><th>Status</th><th>Component</th><th>Evidence</th><th class="num">Tests</th></tr></thead>
      <tbody>{render_components(PIPELINE + ['Platform'], by_stage, totals is not None)}</tbody>
    </table></div>
    <p class="muted small">Test counts: tests whose name or body matches the component's pattern; a test can count
    toward several components.</p>
  </section>

  <div class="grid2" style="margin-top:16px">
    <section><h2>Risks &amp; blockers</h2>{risk_html}</section>
    <section><h2>Open decisions</h2>{decision_html}</section>
  </div>

  <section>
    <h2>Next milestones to pilot</h2>
    <p class="muted small">Proposed, derived from the blockers above; ordered so every milestone follows what it needs.
    Items in the same wave can run in parallel.</p>
    <ol class="milestones">{ms_html}</ol>
  </section>

  <section>
    <h2 id="commits-title">Commits per day, last 30 days</h2>
    <div class="scroll"><div class="chart-scroll">{chart_svg}</div></div>
    <details><summary>Table view</summary><table style="min-width:0;max-width:320px">
      <thead><tr><th>Date</th><th class="num">Commits</th></tr></thead><tbody>{chart_table}</tbody></table></details>
  </section>

  <section>
    <h2>Context vs code</h2>
    <p class="muted small">Claims supplied with this report's brief, checked against the repository.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Claim</th><th>Verdict</th><th>What the repo shows</th></tr></thead>
      <tbody>{context_html}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Inventory</h2>
    <details><summary>Modules ({len(inv['modules'])})</summary><ul class="cols">{modules_html}</ul></details>
    <details><summary>Test functions by file ({len(data['tests'])})</summary><ul>{tests_html}</ul></details>
    <details><summary>Docs ({len(inv['docs'])})</summary><ul>{docs_html}</ul></details>
    <details><summary>Configs &amp; schema</summary><ul>
      {''.join(f"<li><code>{esc(f)}</code></li>" for f in inv['configs'])}
      <li>{len(inv['migrations'])} migrations (latest <code>{esc(inv['migrations'][-1] if inv['migrations'] else 'none')}</code>)</li>
      <li>{len(inv['templates'])} templates</li></ul></details>
    <details><summary>TODO / FIXME / XXX / HACK ({len(inv['todos'])})</summary><ul>{todos_html}</ul></details>
    <details><summary>Tests not mapped to a component ({len(data['unmapped'])})</summary><ul>{unmapped_html}</ul></details>
    <details><summary>Last 30 commits</summary><ul>{commits_html}</ul></details>
  </section>

  <footer>Regenerate with <code>python scripts/stage_report.py</code>. Self-contained: no external assets.</footer>
</div>
<div id="tip" role="tooltip"></div>
<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------

SHORT = {"Booking": "Booking", "Fare Monitoring": "Monitoring", "Reshop Evaluation": "Evaluation",
         "Gate": "Gate", "CONFIRM": "CONFIRM", "Execution": "Execution", "Fee Capture": "Fees"}
VERDICT_PRIORITY = ["Contradicted", "Partly contradicted", "Conflicts with current design", "Code at $20", "Qualified"]


def terminal_summary(data):
    """Exactly 10 lines: tests, % by stage, 3 blockers, 4 discrepancies, unverifiable claims."""
    s_by = {s["stage"]: s for s in data["summaries"]}
    pct = " · ".join(f'{SHORT[st]} {s_by[st]["pct"] if s_by[st]["pct"] is not None else "?"}%' for st in PIPELINE)
    t = data["totals"]
    tests = f'{t["passed"]}/{t["tests"]} passed, {t["failures"]} failed, {t["errors"]} errors' if t else "Unknown (not run)"
    top = [r for r in data["risks"] if r["sev"] in ("critical", "serious")][:3]
    disc = sorted((c for c in data["context"] if c["verdict"] in VERDICT_PRIORITY),
                  key=lambda c: VERDICT_PRIORITY.index(c["verdict"]))
    unknown = [c["claim"] for c in data["context"] if c["verdict"] == "Unknown"]
    lines = [f"TripDifference @ {data['git']['head']} — tests {tests}", f"Complete: {pct}"]
    lines += [f"Blocker {i + 1}: {top[i]['title']}" if i < len(top) else f"Blocker {i + 1}: none" for i in range(3)]
    lines += [f"Discrepancy — {c['claim']}: {c['verdict']}" for c in disc[:4]]
    lines += ["Not verifiable from repo: " + ("; ".join(unknown) if unknown else "none")]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--junit", type=Path, help="existing pytest --junitxml file to reuse")
    ap.add_argument("--skip-tests", action="store_true", help="don't run pytest; results shown as Unknown")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    junit = args.junit
    if not junit and not args.skip_tests:
        junit = run_pytest()
    results, totals = ({}, None)
    if junit and junit.exists():
        results, totals = load_junit(junit)

    tests = test_functions()
    unmapped = resolve_components(tests, results)
    gs = git_state()
    inv = inventory()
    summaries = [stage_summary(s) for s in PIPELINE + ["Platform"]]
    data = {
        "git": gs, "totals": totals, "inventory": inv, "tests": tests, "unmapped": unmapped,
        "summaries": summaries, "risks": risks(totals, gs, summaries),
        "context": context_checks(inv, gs), "milestones": topo(MILESTONES),
        "chart": render_chart(gs["days"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(data))
    print(f"wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}", file=sys.stderr)
    print(terminal_summary(data))


if __name__ == "__main__":
    main()
