# Duffel API validation harness

A spike, not production code. Exercises the Duffel API end to end in test mode
to find out what actually works — in particular whether `change_total_amount`
comes back **negative** when a fare drops.

The real deliverable is [FINDINGS.md](FINDINGS.md). This file is just how to run it.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then put your duffel_test_ token in it
```

`.env` and `responses/` are gitignored.

The harness **refuses to start** on a token that doesn't begin with
`duffel_test_`, and calls out `duffel_live_` explicitly. Test mode only.

## Commands

```bash
.venv/bin/python duffel.py search LHR JFK 2026-10-15   # list offers
.venv/bin/python duffel.py book <offer_id>             # create order, print PNR
.venv/bin/python duffel.py show <order_id>             # full order detail
.venv/bin/python duffel.py reshop <order_id>           # re-search + price the change, print delta
.venv/bin/python duffel.py change <order_id> <slice_id># order change request + change offers
.venv/bin/python duffel.py confirm-change <oco_id>     # execute the exchange
.venv/bin/python duffel.py cancel <order_id>           # refund quote only
.venv/bin/python duffel.py cancel <order_id> -y        # actually cancel
```

Every raw response is dumped to `./responses/<timestamp>-<label>.json`.

## Reshop simulator

Duffel sandbox hardcodes `change_total_amount` to `+125.00` and cannot produce a
price drop (FINDINGS.md §1). So the price source is an interface with two
implementations, and the engine cannot tell which one it has:

```
prices.py    PriceSource
             ├── DuffelPriceSource      live sandbox calls — what ships
             └── SimulatedPriceSource   reads simulated_prices.json, forces any delta
engine.py    evaluate(order, source, policy) -> Decision   + decisions.log (JSONL)
app.py       Flask test rig on :8000
```

Select with `PRICE_SOURCE=duffel|simulated` (defaults to `simulated`, the safe one).

```bash
.venv/bin/python -m pytest test_engine.py -v    # 28 tests, all vs the simulated source
.venv/bin/python app.py                          # → http://localhost:8000
```

**The rule the engine exists to enforce:** `change_total_amount` is the only
number that decides anything. A cheaper market re-search proves nothing — it is a
different number computed a different way, and acting on it books a loss. The
engine records the market delta for diagnostics and never branches on it.

To force a drop, edit `simulated_prices.json` or use the per-order simulate
control in the UI. Set `change_total` negative and re-run the cycle.

Nothing that changes or cancels an order executes without a two-step
confirmation, and the UI refuses to execute at all when the last decision came
from the simulated source.

## Web UI

```bash
.venv/bin/python app.py     # → http://localhost:8000
```

Flask, server-rendered, no build step. Binds to `127.0.0.1` only. The
**`DUFFEL_TOKEN` stays server-side** — never put it in client-side code.

**Employee flow:** Search → Results → Passenger → Payment → Confirmation, then
My trips and a trip detail page with a monitoring toggle and price-history chart.

**Ops console** (`/orders`) keeps the simulate controls, live cycles, and the
two-step exchange/cancel gate. `/decisions` renders `decisions.log`.

Login and Activate (`/login`, `/activate`) are **presentation screens only** —
they gate nothing. This is a single-operator rig and real auth would just get in
the way.

Two things worth knowing:

- The **Monitored / Not monitored** tag on search results comes from the offer's
  `conditions.change_before_departure.allowed`, the only changeability signal
  that exists *before* booking. That field is unreliable on orders
  (FINDINGS.md §8), so treat the tag as a prediction. The authoritative check
  (`available_actions`) runs once the order exists, and drives the trip page.
- **Round-trip books fine, but the engine evaluates the first slice only.**
  `OrderSnapshot` reads `slices[0]`, so a return leg is not re-priced. Fine for
  one-way testing; needs work before round-trips are monitored for real.

`server.py` and the old root `index.html` are superseded by the Flask app.

**Logo:** `logo.png` (512×512) is the badge cropped out of the original
`tripdifference.png` (1920×1080, mostly whitespace). To regenerate it after
changing the source art:

```bash
.venv/bin/python -c "
from PIL import Image
im = Image.open('tripdifference.png').convert('RGB')
im.crop((664, 238, 1265, 839)).resize((512, 512), Image.LANCZOS).save('logo.png', optimize=True)"
```

If `logo.png` is missing the page falls back to a brand-matched SVG stand-in.

## Which carriers support the change flow

**Verified against the live test API — both of these work:**

| Carrier | Change flow | Notes |
|---|---|---|
| Duffel Airways (`ZZ`) | ✅ 11 change offers | Duffel's deterministic sandbox airline |
| American Airlines (`AA`) | ✅ 11 change offers | identical pricing to ZZ |

Contrary to expectation, real carriers in sandbox **do** support order changes.
Duffel appears to serve the same synthetic change stub regardless of carrier, so
there's no false-negative risk in carrier selection — and no carrier gives more
realistic change pricing than any other.

`LAS → LAX` works fine (29 offers). So does `LHR → JFK` (231 offers).

Documented route-triggered behaviours, narrower than they sound:

| Route | Behaviour |
|---|---|
| LHR → LTN | *airline-initiated* changes — a **different** endpoint from the customer-initiated flow |
| LTN → SYD | cancellation refunds to `airline_credits` |

> ⚠️ **`change_total_amount` is hardcoded to `+125.00 USD` in test mode** and
> never goes negative, regardless of carrier, route, cabin, price, or date.
> Read [FINDINGS.md §1](FINDINGS.md) before building on it.

## Design notes

- All money parsed as `Decimal`, never `float`. Duffel returns amounts as strings.
- 429 handling honours `Retry-After` if present, else parses `ratelimit-reset`
  (an RFC 2616 **date**, not a seconds count). Retries once, then gives up.
- No retry framework, no client abstraction. One `api()` helper, one function
  per command, ~400 lines total.

## Deployment

| Environment | URL | Access |
|---|---|---|
| Production | https://tripdifference.vercel.app | **public — no authentication** |
| Staging | https://tripdifference-staging.vercel.app | Vercel SSO (team members only) |

Production tracks `main`, staging tracks the `staging` branch. Deploy with
`vercel --prod --yes` and `vercel --yes` respectively.

Vercel auto-detects the Flask `app` in root `app.py` and routes every path to a
single function — no rewrites. Two things that will bite you if changed:

- **`.vercelignore` patterns match at any depth.** An entry of `index.html`
  silently excluded `templates/index.html` and every page 500'd with
  `TemplateNotFound`. Root-only excludes are anchored with a leading `/`.
- **`responses/` must stay excluded.** At 220MB it blows the 225MB function
  bundle limit on its own.

Static assets live in `public/` (CDN-served). Flask's `static_folder` is not
used — the platform docs advise against it. `/logo.png` has a local-dev route so
the same URL works in both environments.

### ⚠️ Ephemeral storage

`orders.json`, `decisions.log` and `simulated_prices.json` are written to
`DATA_DIR`, which is `/tmp` on Vercel. **That resets when the instance
recycles.** Bookings create real Duffel sandbox orders that continue to exist at
Duffel, but our local record of them disappears. Fine for a demo URL; not
suitable for real bookkeeping without swapping the store for a database.

### ⚠️ Production is unauthenticated

The login screen is decorative (see above) and production is publicly
reachable, so anyone with the URL can book, cancel and run reshop cycles against
the sandbox balance, and can read the ops console and decision log. Preview
deployments are SSO-gated by default; production is not. To gate it, set
Deployment Protection to *Standard Protection* in Project Settings.
