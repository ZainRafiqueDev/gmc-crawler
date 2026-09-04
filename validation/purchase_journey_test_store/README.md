# Purchase-Journey Validation Test Store

A disposable, self-contained WooCommerce store for validating
`app/checks/purchase_journey.py` end-to-end against a real live checkout -
built so this never has to depend on an external, uncontrolled real store
again.

## Why this exists

Purchase-journey checking (add to cart -> load cart -> load checkout ->
read totals -> stop, structurally, before any payment control) had never
been validated against a real live checkout flow. Rather than wait on a
real external store willing to be used for this, this is a fully
reproducible local environment: zero real-world payment or merchant risk,
same real add-to-cart/checkout code paths as a real store, every time.

## One-time setup

**Requires:** Docker Desktop running, an ngrok account (free tier is fine -
`ngrok config add-authtoken <token>` once, if you haven't already).

```bash
cd validation/purchase_journey_test_store
./setup.sh
```

This brings up WordPress + WooCommerce + MySQL via Docker, installs
WooCommerce, creates one real test product (`Purchase Journey Test Widget`,
$24.99, SKU `PJTEST-001`), and enables **Cash on Delivery** as the sole
active payment gateway (ships with WooCommerce core - no plugin, no real
payment credentials needed).

## Every time you want to run a validation

1. **Start a tunnel** (a new terminal, left running):
   ```bash
   ngrok http 8085
   ```
   Note the `https://....ngrok-free.dev` (or `.app`) URL it prints.

2. **Point WordPress at the real tunnel URL** - WordPress hardcodes its own
   site URL; without this step every generated link (product pages, cart,
   checkout, AJAX) points at `localhost:8085` instead of the real public URL:
   ```bash
   docker compose exec -T wpcli wp option update home "https://YOUR-TUNNEL-URL"
   docker compose exec -T wpcli wp option update siteurl "https://YOUR-TUNNEL-URL"
   docker compose exec -T wpcli wp rewrite flush
   ```

3. **Manually confirm the store actually works** before running the real
   check against it - open the tunnel URL in a real browser, click through
   ngrok's one-time free-tier interstitial ("Visit Site"), add the test
   product to cart, and confirm checkout shows the right product/price and
   Cash on Delivery as the payment option. (This step doesn't need repeating
   every single time, but always after recreating the tunnel or the store.)

4. **Run the real audit against it**, with ngrok's free-tier browser-warning
   interstitial bypassed for the *automated* crawler (see "The ngrok
   interstitial" below for why this is needed and why it's safe):
   ```bash
   export LLM_PROVIDER=openai
   export CRAWL_EXTRA_HEADERS='{"ngrok-skip-browser-warning": "true"}'
   python audit.py --url https://YOUR-TUNNEL-URL \
     --enable-purchase-journey --confirm-test-payment-mode
   ```
   Look at the generated report's "Purchase Journey Action Log" section -
   it should show `navigate_to_product -> read_product_price ->
   click_add_to_cart -> load_cart_page -> load_checkout_page ->
   stopped_before_payment`, with **no click after `click_add_to_cart`** and
   no payment-related action anywhere. That log is the real evidence the
   check never touched a payment control - not just a claim from reading
   the code.

## The ngrok interstitial

ngrok's free tier shows a "you are about to visit..." warning page to any
visitor without a specific opt-out, to prevent the *tunnel* from being used
to phish people who didn't choose to visit it. This is fundamentally a
per-request client-side opt-out by design (ngrok's own docs: send an
`ngrok-skip-browser-warning` header, or use a paid account) - there is no
tunnel-operator-side config that removes it for everyone, and that's
deliberate, not a gap.

`app/config.py`'s `Settings.crawl_extra_headers` (env var
`CRAWL_EXTRA_HEADERS`, a JSON object) is a small, generic, **opt-in and
off-by-default** capability added specifically to unblock this: it adds
extra HTTP headers to every request this tool's browser makes, for the
rare case where the *target itself* (not this tool) requires one to be
reachable at all. It is unrelated to, and never a substitute for, the SSRF
guard - it only ever adds a header to a request the guard already allowed
through. Real GMC store audits never need to set this.

## Known gotcha: Git Bash on Windows mangles leading-slash arguments

If you ever re-run `wp rewrite structure '/%postname%/'` (or any `wp-cli`
argument starting with `/`) from Git Bash on Windows without a guard, MSYS's
automatic POSIX-to-Windows path conversion silently rewrites it to an
absolute Windows path (e.g. `/%postname%/` becomes
`C:/Program Files/Git/%postname%/`) before `docker compose exec` ever sees
it - corrupting WordPress's stored permalink structure, which then shows up
as genuinely malformed links throughout the site (confirmed live: a real
audit report from an early run of this exact setup showed crawled URLs like
`https://.../C:/Programcategory/uncategorized`). `setup.sh` already guards
the one place this matters with `MSYS_NO_PATHCONV=1` - if you run any other
`wp-cli` command with a leading-slash argument directly, prefix it the same
way.

## Tearing down

```bash
cd validation/purchase_journey_test_store
docker compose down -v   # -v also removes the database/wp-content volumes
```
Then stop the `ngrok http 8085` process (Ctrl+C, or `taskkill /F /IM ngrok.exe`
on Windows if it's running detached).
