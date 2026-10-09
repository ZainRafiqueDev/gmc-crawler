# Canonical Fact Dictionary (First Audit "Digital Twin")

Locked format, same purpose as `app/rules/starter_rules.yaml`: every extractor
(generalized `business_identity`, new shipping/returns/payment extractors,
JSON-LD) emits `app.db.ResourceFact` rows in exactly this shape, and the
contradiction engine (work-order step 4, piece 3) compares ONLY on
`(fact_key, normalized value)` - never on raw text. No extractor code is
written against this yet; this file is the thing being locked before any is.

## How to read a row

- **fact key** - the canonical string stored in `ResourceFact.fact`.
- **value type** - the logical type; `ResourceFact.value` is always a string
  column, so this is how that string is encoded/decoded, not a second column.
- **normalization rule** - how raw page text becomes that value. Any rule not
  marked otherwise also gets the engine-wide default: whitespace-collapsed,
  case-insensitive comparison (so "ACME Inc" and "acme inc" are the same
  value without a comment on every row).
- **expected pages** - which `PageType`(s) the extractor looks at. A fact
  found on a page not listed here isn't an error - it just means that page
  carries a stronger-than-usual signal worth noting if it happens live.
- **comparison scope** - `site-wide` (compare this key's values across many
  different Resources - a true cross-page contradiction) or `per-resource`
  (compare this key's values across different *extraction methods* for the
  *same* Resource - e.g. JSON-LD vs. platform API for one product).
- **method tag(s)** - the `ResourceFact.method` value(s) that can produce
  this key. Each method has a fixed base confidence (table below); a fact's
  actual `confidence` is that base value, not hand-tuned per instance.

## Method -> base confidence (not per-fact - per extraction method)

| method | base confidence | why |
|---|---|---|
| `platform_api` | 0.98 | WooCommerce REST / Shopify `products.json` - authoritative source, already used by `app.checks.product_checks` |
| `jsonld` | 0.95 | machine-readable structured data (JSON-LD) |
| `regex_llm_agree` | 0.95 | regex and LLM independently extracted the same value - two-signal structural agreement |
| `business_identity_regex` | 0.90 | existing, already-shipped email/phone/address regex (`app.checks.business_identity`) |
| `llm` | 0.75 default, but see note below | semantic extraction (`app.facts.llm_commerce_facts`) - the one method whose confidence is NOT fixed; see "LLM-assisted facts" below |
| `shipping_time_regex` / `return_window_regex` / `shipping_cost_regex` / `shipping_region_regex` | 0.80 | numeric/pattern regex over free text - reliable but not machine-readable |
| `copyright_footer_regex` / `legal_suffix_regex` / `heading_heuristic` | 0.75 | heuristic text-pattern match, more variation in real-world phrasing |
| `refund_terms_keyword_classifier` / `return_cost_keyword_classifier` / `payment_methods_keyword_regex` | 0.70 | keyword classification over prose - real false-positive surface |
| `calling_code_cross_reference` / `address_text_country_regex` | 0.60 | inferential (a phone country code or a country *mention*, not a declared field) |

**Confidence stays first-class, threshold stays configurable**: a
contradiction only becomes a `fail` `Evaluation`/`Finding` when the *lower*
of the two (or more) facts being compared meets or exceeds that **rule's own
`min_confidence`** (already in `app/rules/schema.py` from step 3 - no new
config). Below it, `Evaluation.result = "needs_review"` - the rule-engine
equivalent of the existing `Confidence.CANNOT_VERIFY` pattern used elsewhere
in the pipeline (soft-404 downgrade, `evidence_verified`), kept as its own
small string enum on `Evaluation` rather than importing `Finding`'s
`Confidence` type, since an `Evaluation` isn't itself a `Finding`.

---

## Shared normalization helpers (locked before any extractor code - `app/facts/normalize.py`)

These are shared across every extractor that touches the same kind of raw
value, so a cosmetic difference in how two sources phrase the same fact
never reads as a contradiction.

### Availability - one canonical enum, two real-world vocabularies

Canonical enum: `in_stock` | `out_of_stock` | `preorder` | `unspecified`.
Both of the following map into it - **never compared as raw strings against
each other**, which is exactly the `InStock` vs `instock` false-flag this
locks down:

| source vocabulary | raw value | canonical |
|---|---|---|
| schema.org `Offer.availability` (JSON-LD; full URL or last path segment, case-insensitive) | `InStock`, `LimitedAvailability` | `in_stock` |
| | `OutOfStock`, `SoldOut`, `Discontinued` | `out_of_stock` |
| | `PreOrder`, `PreSale`, `BackOrder` | `preorder` |
| WooCommerce REST `stock_status` (`app.checks.woocommerce_products`, already fetched - no new call) | `instock` | `in_stock` |
| | `outofstock` | `out_of_stock` |
| | `onbackorder` | `preorder` |
| Shopify `products.json` variant `available` (bool, `app.checks.shopify_products`) | `true` | `in_stock` |
| | `false` | `out_of_stock` |
| (anything else, including absent/unparseable) | - | `unspecified` - never guessed |

### Identifiers - GTIN canonicalized, MPN/SKU normalized, never compared raw

- **GTIN**: strip every non-digit character, then **left-pad with zeros to
  14 digits** (GS1's own equivalence rule - a GTIN-8/12/13 is defined as the
  zero-padded prefix of its GTIN-14 form, so a product's GTIN-12 from one
  source and its GTIN-14 from another are the *same identifier*, not a
  mismatch). No check-digit validation - this only normalizes form, it
  doesn't validate correctness.
- **MPN**: strip every non-alphanumeric character (spaces, dashes,
  underscores), uppercase. Manufacturer part numbers aren't GS1-standardized,
  so no length/padding rule applies - only cosmetic formatting is normalized.
- **SKU**: same rule as MPN (strip non-alphanumeric, uppercase) - merchant-
  defined, same cosmetic-variation risk.

### Country names -> ISO-3166-1 alpha-2

One table, generalized from `business_identity._CALLING_CODE_COUNTRIES`'s
existing reverse lookup (calling code -> country name fragments) into a
standalone `{alpha-2: [name fragments incl. abbreviations like "USA"/"UK"]}`
map, reused by both `business.address_country` and `shipping.region`.

### Money - symbol -> ISO-4217, amount parsed separately from currency

A fixed `{symbol: ISO-4217}` map (`$`->`USD`, `£`->`GBP`, `€`->`EUR`, ...) -
ambiguous symbols shared by multiple currencies (`$` alone, without a
locale/country signal) default to `USD` (the common case for this tool's
target market) rather than guessing a less-likely one; never fabricated when
no symbol is present at all (no currency fact is emitted, not a guessed
default).

### Day ranges -> single int (upper bound)

"N business days", "N-M business days", "same day", "within N hours" all
reduce to one int via the same parser: a stated range's **upper** bound (the
number the merchant is actually committing to), "same day" -> `0`, "within N
hours" -> `1` if `N <= 24` else `ceil(N / 24)`.

## LLM-assisted "free-prose" facts (`return.window_days`, `shipping.processing_days`, `shipping.delivery_days`, `return.refund_terms`)

These four, and only these four, are LLM-assisted (`app/facts/llm_commerce_facts.py`) rather than
purely deterministic. Found live, twice, on modcloth.com: keyword-proximity regex cannot tell what a
number refers to ("processed within 2 business days" is refund-processing speed, not the return
window) and cannot follow a window stated without the literal word "return" nearby ("Requests must
be opened within 30 days..."). Every other commerce fact (`shipping.region`, `shipping.cost.*`,
`payment.methods`, `return.cost_responsibility`) and every business-identity/product/JSON-LD fact
stays purely deterministic - this is a narrow, deliberate carve-out, not a general policy shift.

**Reconciliation** (regex stays as a fast, free cross-check, never discarded):
1. The LLM is asked first. If it says a fact isn't stated on this page at all (`null`), no fact is
   emitted - full stop, regardless of what the regex fast-path found. Trusting the LLM's "nothing
   here" judgment over a regex guess is the entire point of this round.
2. If the LLM found a value and the regex fast-path's own candidate agrees, one fact is emitted with
   method `regex_llm_agree` - a fixed, high confidence (0.95): two independent signals agreeing is
   strong, structural evidence.
3. If the LLM found a value and the regex fast-path disagreed or found nothing, the LLM's value is
   used, tagged method `llm`, with the **LLM's own reported confidence** - not a fixed per-method
   constant. This is the one deliberate exception to "confidence is a pure function of method"
   (`app/facts/keys.py`'s `METHOD_BASE_CONFIDENCE`): a semantic extraction's reliability genuinely
   varies per instance, not per method, so `method="llm"`'s confidence is read straight from the
   model's own structured output (clamped to `[0, 1]`) instead of looked up.
4. The LLM's cited source sentence is verified against the real page text
   (`app.llm.checks.verify_evidence_quote`, reused) before any of the above applies - a claimed
   sentence that isn't actually in the page voids the extraction entirely, same anti-hallucination
   discipline as every other LLM-graded check in this project.
5. No LLM configured (`Settings.llm_configured` is `False`) -> falls back to the regex fast-path
   alone, byte-for-byte the same behavior these four facts had before this round.

## Business identity

| fact key | value type | normalization | expected pages | scope | method(s) |
|---|---|---|---|---|---|
| `business.name` | string | trim + collapse whitespace | HOMEPAGE, CONTACT_ABOUT, PRIVACY_POLICY, TERMS_OF_SERVICE | site-wide | `copyright_footer_regex`, `heading_heuristic` |
| `business.legal_entity` | string (name + legal suffix, e.g. "Acme Inc.") | trim + collapse whitespace; suffix is kept, not stripped - a suffix mismatch ("Acme Inc." vs "Acme LLC") IS the contradiction | PRIVACY_POLICY, TERMS_OF_SERVICE, HOMEPAGE footer | site-wide | `copyright_footer_regex`, `legal_suffix_regex` |
| `business.email` | string (email) | lowercase (reuses `business_identity._EMAIL_RE` + `.lower()` exactly as today) | HOMEPAGE, CONTACT_ABOUT, PRIVACY_POLICY, SHIPPING_POLICY, RETURNS_POLICY, TERMS_OF_SERVICE | site-wide | `business_identity_regex` |
| `business.phone` | string (digits, optional leading `+`) | strip all non-digit/non-`+` chars (reuses `business_identity._normalize_phone_digits` exactly) | same page set as `business.email` | site-wide | `business_identity_regex` |
| `business.address` | string (street-address fragment) | trim/collapse whitespace (reuses `business_identity._ADDRESS_HINT_RE` exactly) | same page set as `business.email` | site-wide | `business_identity_regex` |
| `business.address_country` | ISO-3166-1 alpha-2 (e.g. `"US"`) | free-text country mention ("United States"/"US"/"USA"/"U.S.A") -> one alpha-2 token, via a shared country-name normalizer that generalizes `business_identity._CALLING_CODE_COUNTRIES`'s reverse lookup into its own reusable table (used again by `shipping.region` below) | same page set as `business.email` | site-wide | `calling_code_cross_reference`, `address_text_country_regex` |

## Shipping

| fact key | value type | normalization | expected pages | scope | method(s) |
|---|---|---|---|---|---|
| `shipping.region` | normalized set of ISO-3166-1 alpha-2 codes, encoded as sorted comma-joined string (e.g. `"CA,GB,US"`); sentinel `"WORLDWIDE"` for "we ship worldwide"/"international shipping" | per-country text run through the same country-name normalizer as `business.address_country`; "worldwide"/"global"/"all countries" -> `WORLDWIDE` directly | HOMEPAGE, SHIPPING_POLICY | site-wide | `shipping_region_regex` |
| `shipping.cost.amount` | decimal number as string (e.g. `"9.99"`); `"0"` also means "advertised as free" (GMC's own free-shipping claim is effectively a $0 cost claim - no separate boolean fact for it) | parse a money-like pattern; "free shipping" -> `"0"` | SHIPPING_POLICY, PRODUCT, CHECKOUT | site-wide | `shipping_cost_regex` |
| `shipping.cost.currency` | ISO-4217 (e.g. `"USD"`) | symbol ($/£/€/...) -> ISO-4217 via a fixed symbol map; **absent** (no fact row) when the cost is the free-shipping `"0"` sentinel - never guessed | same pages as `shipping.cost.amount` | site-wide | `shipping_cost_regex` |
| `shipping.processing_days` | int (business days) | upper bound of any stated range: "1-2 business days" -> `2`; "same day" -> `0`; "ships within 24 hours" -> `1` | SHIPPING_POLICY, PRODUCT, FAQ | site-wide | `shipping_time_regex`, `llm`, `regex_llm_agree` |
| `shipping.delivery_days` | int (days) | same range-upper-bound rule as `shipping.processing_days`: "3-5 business days" -> `5` | SHIPPING_POLICY, PRODUCT, FAQ, CHECKOUT | site-wide | `shipping_time_regex`, `llm`, `regex_llm_agree` |

## Returns

| fact key | value type | normalization | expected pages | scope | method(s) |
|---|---|---|---|---|---|
| `return.window_days` | int (days) | "30 days"/"30-day" -> `30`; "within 14 calendar days" -> `14`; "no returns"/"final sale" -> `0` (a real, meaningful value - distinct from "not stated", which simply means no fact row exists at all) | RETURNS_POLICY, FAQ, PRODUCT, CHECKOUT | site-wide | `return_window_regex`, `llm`, `regex_llm_agree` |
| `return.refund_terms` | enum: `full_refund` \| `store_credit_only` \| `exchange_only` \| `partial_refund` \| `unspecified` | keyword classification of the surrounding sentence ("full refund"/"money back" -> `full_refund`; "store credit" -> `store_credit_only`; "restocking fee"/"partial" -> `partial_refund`) | RETURNS_POLICY, FAQ | site-wide | `refund_terms_keyword_classifier`, `llm`, `regex_llm_agree` |
| `return.cost_responsibility` | enum: `merchant_pays` \| `customer_pays` \| `free_above_threshold` \| `unspecified` | "free returns"/"we cover return shipping" -> `merchant_pays`; "customer is responsible for return shipping" -> `customer_pays`; "free returns on orders over $50" -> `free_above_threshold` | RETURNS_POLICY, FAQ | site-wide | `return_cost_keyword_classifier` |

## Payment

| fact key | value type | normalization | expected pages | scope | method(s) |
|---|---|---|---|---|---|
| `payment.methods` | normalized set from a fixed vocabulary (`VISA`, `MASTERCARD`, `AMEX`, `DISCOVER`, `PAYPAL`, `APPLE_PAY`, `GOOGLE_PAY`, `SHOP_PAY`, `KLARNA`, `AFTERPAY`), encoded as sorted comma-joined string; an unrecognized-but-present logo/text keeps an `OTHER:<raw text>` escape token rather than being silently dropped | brand-keyword/logo-alt-text match against the fixed vocabulary | HOMEPAGE footer, CHECKOUT, FAQ | site-wide | `payment_methods_keyword_regex` |

## Product (JSON-LD + visible HTML + platform API - same keys, different methods)

This is the "same fact key where sources overlap" case: a JSON-LD price and
a WooCommerce/Shopify API price for the **same product Resource** both emit
`product.price.amount`/`product.price.currency` - that shared key is what
lets `price_mismatch_jsonld_vs_platform` compare them. Money is split into
`.amount` + `.currency` rather than one composite string specifically so the
rule engine's `numeric_mismatch` condition can do a plain numeric-tolerance
comparison without needing to parse a composite value itself.

| fact key | value type | normalization | expected pages | scope | method(s) |
|---|---|---|---|---|---|
| `product.price.amount` | decimal number as string | parse JSON-LD `price`/`lowPrice` directly (string or numeric, thousands separators stripped); parse visible price text by stripping the currency symbol/thousands separators; platform API's own numeric price field passed through unchanged | PRODUCT | **per-resource** (compare across methods for the same product) | `jsonld`, `visible_price_regex`, `platform_api` |
| `product.price.currency` | ISO-4217 | JSON-LD `priceCurrency` passed through; visible price's symbol -> ISO-4217 via the same symbol map as `shipping.cost.currency`; platform API's own currency field passed through unchanged | PRODUCT | per-resource | `jsonld`, `visible_price_regex`, `platform_api` |
| `product.availability` | enum: `in_stock` \| `out_of_stock` \| `preorder` \| `unspecified` | JSON-LD `availability` (full URL, bare token, or http/https - all equivalent) -> short token via `normalize_availability`, same function platform-API facts use; visible "Add to Cart" present/absent is a weaker, page-only fallback signal | PRODUCT | per-resource | `jsonld`, `visible_availability_heuristic` |
| `product.brand` | string | trim (engine-wide case-insensitive compare applies); JSON-LD `brand` may be a bare string or a `{"@type":"Brand","name":...}` object - both handled | PRODUCT | per-resource | `jsonld` |
| `product.gtin` | string | any of `gtin`/`gtin8`/`gtin12`/`gtin13`/`gtin14` -> `normalize_gtin` (zero-padded GTIN-14) - all map to the same canonical field; trim only otherwise, **no casefold**: identifiers are compared verbatim, never fuzzy-matched | PRODUCT | per-resource | `jsonld` |
| `product.mpn` | string | `normalize_identifier` (strip non-alphanumeric, uppercase) | PRODUCT | per-resource | `jsonld` |
| `product.sku` | string | `normalize_identifier`; one fact row **per offer/variant** when a product page has multiple offers - never collapsed to a single value | PRODUCT | per-resource | `jsonld` |
| `product.condition` | enum: `new` \| `used` \| `refurbished` \| `unspecified` | JSON-LD `itemCondition` (full URL, bare token, or http/https) -> short token via `normalize_condition` | PRODUCT | per-resource | `jsonld` |

### JSON-LD parsing decisions (piece 2, `app/facts/jsonld_facts.py`)

- **Every `<script type="application/ld+json">` block on the page is parsed**, not just the
  first - a page with a `Product` node in one block and an `Organization`/`BreadcrumbList` node in
  another is common, and only the `Product` node(s) are ever used.
- **`@graph` is walked recursively** - a block's top-level JSON may be a single object, a list of
  objects, or `{"@graph": [...]}`; all three (and arbitrary nesting of them) flatten into the same
  list of candidate nodes before filtering to `@type: Product`.
- **`AggregateOffer` emits `lowPrice`** as `product.price.amount` (documented choice, not
  `highPrice` or an average) - `lowPrice` is the price a shopper can actually pay, matching what a
  `platform_api`/visible-price observation would show for the cheapest variant. If the
  `AggregateOffer` itself also nests a real `offers` array (per-variant detail), those are extracted
  too, in addition to the aggregate fact - never instead of it.
- **An `offers` array (real product variants) is never collapsed** - each offer's `price`/
  `availability`/`sku`/`gtin`/`mpn` becomes its own set of fact rows, all under the same
  `resource_id` (one product page), distinguished by `source_text` (prefixed with the offer's SKU
  when available) rather than a dedicated variant column - sufficient to avoid silently discarding
  variants; a dedicated `variant_sku` column is a reasonable future addition if cross-variant
  contradiction detection is ever needed, not built here.
- **A malformed `ld+json` block is caught and skipped**, not fatal - `json.JSONDecodeError` on one
  `<script>` tag never aborts extraction for the rest of the page's blocks.
- **A present-but-empty/null field never becomes a fact** - e.g. `"price": null` or `"sku": ""`
  emits nothing for that field, not a zero-confidence placeholder.

---

## Consequence for the already-locked `starter_rules.yaml` (flagging now, not changing yet)

**Done, as of piece 4 step 1** (was flagged here, deferred until now): the five
rules were renamed to canonical dotted keys, `RuleCondition.numeric_mismatch`
now takes `fact` + `methods` (a pair) instead of `left`/`right`, and
`app.rules.loader` now validates every rule's fact key/resource_type/method
against this dictionary's registry (`app/facts/keys.py`) at load time -
ships atomically with the rename, so there was never a window where a rule
loaded cleanly but referenced a key nothing produces. All 624 pre-existing
tests stayed green through the rename before any new piece-4 logic was
written on top.
