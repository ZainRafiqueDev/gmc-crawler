# Impact tier grounding

How `app/impact_tier.py` decided SUSPENSION_RISK vs LISTING_DISAPPROVAL vs
QUALITY_IMPROVEMENT for every check_id - not an assumption, a real query
against the live Phase C RAG index.

## Method

For each of the 8 indexed policy areas, ran the same query against the real,
live-embedded policy chunk index (`app/llm/policy_rag.py::get_policy_context`,
top_n=4):

> "What happens if a merchant violates this policy - is the individual
> product disapproved, or can the entire Google Merchant Center account be
> suspended?"

The full, unedited retrieved text for all 8 areas (all `from_real_index: True`
- confirmed real index hits, not the stub fallback) is in
`impact_tier_grounding_raw.txt` next to this file, reproducible with
`validation/research_impact_tiers.py`.

## What the retrieved text actually said

- **misrepresentation** (`answer/6150127`): explicit, unambiguous - *"your
  Google accounts will be suspended upon detection and without prior
  warning."* Its "Best practices" section names business description,
  contact info, and own-branding consistency by name. Its section titled
  "Unavailable offers" carries the same suspend-without-warning language.
  → grounds business-identity-consistency, missing required pages, and
  non-functional cart/checkout as **suspension_risk**. Reconsidered
  `external_domain_link` out of this bucket despite the brief's own starting
  hypothesis naming it: the "own branding" best practice describes a
  site-wide identity/branding *pattern*, but the check fires once per
  individual external link found (including entirely benign ones, like a
  social-media footer icon) - tagging every single instance as
  suspension-risk would be a mismatch between a per-link finding and a
  site-wide policy concern, so it's defaulted to **listing_disapproval**
  (ambiguous) instead.

- **prohibited_content** (`answer/6149970`): disapproves individual ads by
  default, but explicitly extends to *"suspending accounts for repeat or
  egregious violations."* → **suspension_risk** (matches this project's own
  historical treatment of prohibited content as the more severe class).

- **shipping_policy** (`answer/6324484`): entirely item-level - *"we'll
  disapprove your product"* / *"your product or account could be
  disapproved"* (the account mention is conditional/secondary, not the
  primary consequence). No suspend-without-warning language anywhere in the
  retrieved text. → **listing_disapproval**, extended to the other
  per-item pricing/availability-accuracy checks as the closest indexed
  grounding (GMC's separate product-data-specification policy isn't one of
  this project's 8 indexed areas).

- **editorial_quality** (`answer/12079604`): *"Products that don't comply...
  may be disapproved"* - no suspension language anywhere for thin/duplicate
  content. → **quality_improvement**.

- **privacy_policy** / **terms_of_service**: both indexed from
  `answer/13693195` ("Account suspension caused by policy violations"),
  which names what's checked before a suspension re-review: *"verifying the
  accuracy and consistency of product details, business information,
  policies, and contact information."* Policies (plural) sit in the same
  trust-signal cluster as business info - grounds **required_page_present**
  (a policy/contact page missing entirely) as **suspension_risk** across all
  five of its page types (privacy/shipping/returns/ToS/contact), even though
  shipping_policy's own specific text is disapproval-only for *shipping-data
  accuracy on existing pages* - a different question (page absent vs. page
  content wrong).

- **business_identity** (`answer/17123687`): this indexed page is actually
  about Merchant Center's *identity-verification document upload process*,
  not enforcement consequences - not useful grounding on its own. The real
  grounding for business-identity-consistency findings comes from
  misrepresentation's "Best practices" section instead (see above).

- **returns_refunds** (`answer/15625417`): the indexed page is just a
  one-line policy description, no enforcement-consequence text retrieved for
  this query. No independent grounding found; required_page_present's
  suspension_risk classification (via the general "policies" trust-signal
  reasoning above) is what actually applies here.

## Ambiguous check_ids (no direct textual grounding - defaulted per instructions)

`app.impact_tier.AMBIGUOUS_CHECK_IDS` lists every check_id defaulted to
LISTING_DISAPPROVAL for lack of a direct quote, rather than guessed into
QUALITY_IMPROVEMENT: generic broken-link/image/HTTPS/duplicate-boilerplate
checks, product-image mechanics, `llm_image_product_mismatch`, and the
purchase-journey checks that are always `CANNOT_VERIFY` by construction
(`purchase_journey_blocked_ssrf`, `llm_image_vision_check`) - the latter two
are defaulted because they represent "we couldn't check," not a confirmed
violation, independent of any policy-text question.

The full check_id → tier → citation table lives in `app/impact_tier.py`
(`_CLASSIFICATION`), not duplicated here, so there's exactly one place to
update if a check changes or new policy text is found.
