"""Classify a crawled page into a PageType using URL patterns plus heading/
body-text signals - filenames alone are unreliable (e.g. a page titled
"Refund & Returns Policy" living at /policies/ or /help/returns-info).

Language coverage (Part 3, broader real-world crawl robustness round): the
URL rules below are largely language-agnostic in practice for the required
policy-page types, since many translated stores keep English URL slugs
(from the plugin/theme default, or for SEO). The TEXT rules were English-
only, which meant a store whose slugs *and* content are localized (e.g. a
French store at /politique-de-confidentialite/) fell through to
BLOG_OTHER - and app.checks.deterministic.check_required_pages would then
report a confident "missing" finding for a page that genuinely exists,
purely because this classifier couldn't read it. Confirmed live against a
real translated WooCommerce store before adding this (see the validation
notes for this round). Fixed two ways: localized URL-slug and text patterns
for the handful of languages below, plus (in app.checks.deterministic) a
downgrade from a confident "missing" to "could not verify" when the site's
detected content language isn't one of them - full multilingual coverage
beyond this is an explicit, scoped-out follow-up, not claimed as complete.
"""
from __future__ import annotations

import re

from app.models import PageType

# Languages this classifier has real URL/text pattern coverage for, beyond
# English - not exhaustive (a genuine full multilingual build is a follow-up,
# see the module docstring), but covers the required-page types' most common
# terms in several major storefront languages. Used by
# app.checks.deterministic to decide whether a "missing" verdict for a site
# in some *other* language is confident or should be downgraded to
# "could not verify" instead of guessed.
SUPPORTED_LANGUAGES = {"en", "es", "fr", "de", "pt", "it"}

# (PageType, url regexes, heading/text regexes) - order matters, first match wins.
_URL_RULES: list[tuple[PageType, list[str]]] = [
    (PageType.CART, [r"/cart/?($|\?)", r"/panier/?($|\?)", r"/warenkorb/?($|\?)", r"/carrito/?($|\?)", r"/carrello/?($|\?)"]),
    (PageType.CHECKOUT, [r"/checkout/?($|\?)", r"/commande/?($|\?)", r"/kasse/?($|\?)"]),
    # Account-flow pages (WooCommerce's default /my-account slug, plus
    # generic login/register) - classified explicitly, ahead of the
    # text-based fallback below. Found live: WooCommerce's own default
    # login/register page renders a privacy-policy consent notice
    # ("...for other purposes described in our privacy policy.") right in
    # its body text, which the text-based PRIVACY_POLICY rule matched on -
    # misclassifying the account page itself as the store's privacy policy
    # page. A URL match here short-circuits classify_page before text
    # rules ever run, the same way /cart and /checkout already do.
    (PageType.BLOG_OTHER, [r"/my-account", r"/login/?($|\?)", r"/register/?($|\?)", r"/sign-in", r"/sign-up", r"/mon-compte", r"/mein-konto", r"/mi-cuenta"]),
    (
        PageType.PRIVACY_POLICY,
        [
            r"privacy[-_]?policy", r"/privacy/?($|\?)",
            r"politique[-_]?de[-_]?confidentialit", r"protection[-_]?des[-_]?donn",       # fr
            r"datenschutz",                                                                # de
            r"politica[-_]?de[-_]?privacidad", r"aviso[-_]?de[-_]?privacidad",             # es
            r"politica[-_]?de[-_]?privacidade",                                            # pt
            r"informativa[-_]?sulla[-_]?privacy", r"privacy[-_]?policy",                   # it
        ],
    ),
    (
        PageType.SHIPPING_POLICY,
        [
            r"shipping[-_]?policy", r"/shipping/?($|\?)", r"delivery[-_]?(policy|info)",
            r"shipment[-_]?policy",                                                            # a real theme's own synonym for "shipping policy" - found live (vellano.site)
            r"livraison", r"politique[-_]?d.exp.dition",                                    # fr
            r"versand(kosten|bedingungen|richtlinie)?",                                     # de
            r"politica[-_]?de[-_]?env.o", r"/envios?/?($|\?)",                              # es
            r"politica[-_]?de[-_]?envio", r"/entrega/?($|\?)",                              # pt
            r"politica[-_]?di[-_]?spedizione", r"/spedizion",                               # it
        ],
    ),
    (
        PageType.RETURNS_POLICY,
        [
            r"return|refund",
            r"retour|remboursement",                                                        # fr
            r"r.ckgabe|widerruf|erstattung",                                                 # de
            r"devoluci.n|reembolso",                                                         # es
            r"devolu..o|reembolso",                                                          # pt
            r"reso|rimborso",                                                                # it
        ],
    ),
    (
        PageType.TERMS_OF_SERVICE,
        [
            r"terms[-_]?(of[-_]?(service|use)|and[-_]?conditions)?", r"/tos/?($|\?)",
            r"conditions[-_]?g.n.rales", r"mentions[-_]?l.gales",                            # fr
            r"allgemeine[-_]?gesch.ftsbedingungen", r"/agb/?($|\?)", r"impressum",           # de
            r"t.rminos[-_]?y[-_]?condiciones", r"aviso[-_]?legal",                           # es
            r"termos[-_]?e[-_]?condi..es", r"termos[-_]?de[-_]?uso",                         # pt
            r"termini[-_]?e[-_]?condizioni",                                                 # it
        ],
    ),
    (PageType.FAQ, [r"/faq", r"foire[-_]?aux[-_]?questions", r"h.ufige[-_]?fragen", r"preguntas[-_]?frecuentes", r"perguntas[-_]?frequentes", r"domande[-_]?frequenti"]),
    (
        PageType.CONTACT_ABOUT,
        [
            r"/contact", r"/about",
            r"/nous[-_]?contacter|/.[-_]?propos",                                            # fr
            r"/kontakt", r"/.ber[-_]?uns",                                                   # de
            r"/contacto", r"/sobre[-_]?nosotros|/qui.nes[-_]?somos",                         # es
            r"/contato", r"/sobre[-_]?n.s",                                                  # pt
            r"/contatt", r"/chi[-_]?siamo",                                                  # it
        ],
    ),
    (PageType.CART, [r"/basket/?($|\?)"]),
    (
        PageType.PRODUCT,
        [
            r"/product/", r"/products/(?!$)", r"/shop/[^/]+/?$",
            r"/produit/",                                                                    # fr
            r"/produkt/",                                                                     # de
            r"/producto/",                                                                    # es
            r"/produto/",                                                                     # pt
            r"/prodotto/",                                                                    # it
        ],
    ),
    (
        PageType.COLLECTION,
        [
            r"/shop/?($|\?)", r"/collections/", r"/category/", r"/product-category/",
            r"/categorie-produit/",                                                           # fr (WooCommerce's own French default slug)
            r"/produktkategorie/",                                                             # de
            r"/categoria-producto/",                                                           # es
            r"/categoria-produto/",                                                            # pt
            r"/categoria-prodotto/",                                                           # it
        ],
    ),
    (PageType.BLOG_OTHER, [r"/blog/", r"/news/"]),
]

_TEXT_RULES: list[tuple[PageType, list[str]]] = [
    (
        PageType.PRIVACY_POLICY,
        [
            r"privacy policy", r"personal (data|information) we collect",
            r"politique de confidentialit", r"donn.es personnelles",                         # fr
            r"datenschutzerkl.rung", r"datenschutzrichtlinie",                                # de
            r"pol.tica de privacidad", r"datos personales",                                   # es
            r"pol.tica de privacidade", r"dados pessoais",                                    # pt
            r"informativa sulla privacy", r"dati personali",                                  # it
        ],
    ),
    (
        PageType.SHIPPING_POLICY,
        [
            r"shipping policy", r"delivery times?", r"shipping (rates|costs|options)", r"shipment policy",
            r"politique de livraison", r"d.lais de livraison|frais de livraison",             # fr
            r"versandkosten|lieferzeiten|versandrichtlinie",                                  # de
            r"pol.tica de env.o", r"tiempos de entrega|gastos de env.o",                      # es
            r"pol.tica de envio", r"prazos de entrega",                                       # pt
            r"politica di spedizione", r"tempi di consegna",                                  # it
        ],
    ),
    (
        PageType.RETURNS_POLICY,
        [
            r"return(s)? (policy|process)", r"refund policy", r"how to return",
            r"politique de retour", r"politique de remboursement",                            # fr
            r"r.ckgaberecht|widerrufsrecht|erstattungsrichtlinie",                             # de
            r"pol.tica de devoluci.n", r"pol.tica de reembolso",                               # es
            r"pol.tica de devolu..o", r"pol.tica de reembolso",                                # pt
            r"politica di reso", r"politica di rimborso",                                      # it
        ],
    ),
    (
        PageType.TERMS_OF_SERVICE,
        [
            r"terms (of service|and conditions)", r"terms of use",
            r"conditions g.n.rales de vente", r"mentions l.gales",                             # fr
            r"allgemeine gesch.ftsbedingungen",                                                # de
            r"t.rminos y condiciones", r"aviso legal",                                          # es
            r"termos e condi..es", r"termos de uso",                                            # pt
            r"termini e condizioni",                                                            # it
        ],
    ),
    (PageType.FAQ, [r"frequently asked questions", r"foire aux questions", r"h.ufig gestellte fragen", r"preguntas frecuentes", r"perguntas frequentes", r"domande frequenti"]),
    (
        PageType.CONTACT_ABOUT,
        [
            r"contact us", r"get in touch", r"about us",
            r"nous contacter", r"contactez[-_]?nous|. propos de nous",                         # fr
            r"kontaktieren sie uns|.ber uns",                                                   # de
            r"cont.ctenos|sobre nosotros",                                                      # es
            r"entre em contato|sobre n.s",                                                      # pt
            r"contattaci|chi siamo",                                                            # it
        ],
    ),
    (PageType.CART, [r"your (shopping )?cart", r"\bcart is empty\b"]),
    (PageType.CHECKOUT, [r"proceed to checkout", r"billing details", r"place order"]),
    (PageType.PRODUCT, [r"add to cart", r"add to bag"]),
]


# Page types worth prioritizing during crawl (site_mapper.py, hardening
# round section 3.3; split into two tiers per the Store-Overview-first
# restructuring): a real GMC audit cares much more about reaching these than
# about the Nth collection/category page - large-catalog stores with many
# top-level collections can otherwise burn the entire page budget on
# collections before ever reaching a product or policy page (observed live
# on a real Shopify store with ~40 top-level collections).
#
# Store Overview pages (homepage, policy pages, business-identity/contact)
# are seeded ahead of Catalog pages (products) - not just ahead of generic
# collection/blog pages - so crawl budget spent on-site structure/trust
# signals always wins over catalog depth, per the report's own Store
# Overview vs Catalog split (app/report.py).
_OVERVIEW_PRIORITY_TYPES = {
    PageType.PRIVACY_POLICY, PageType.SHIPPING_POLICY,
    PageType.RETURNS_POLICY, PageType.TERMS_OF_SERVICE, PageType.CONTACT_ABOUT,
}
_CATALOG_PRIORITY_TYPES = {PageType.PRODUCT}


def _matches_any(path: str, page_types: set[PageType]) -> bool:
    lowered = path.lower()
    for page_type, patterns in _URL_RULES:
        if page_type in page_types and any(re.search(p, lowered) for p in patterns):
            return True
    return False


def looks_like_overview_priority_url(path: str) -> bool:
    """Cheap URL-only pre-check (no page content available yet) for whether
    a URL looks like a Store Overview page (policy/contact) - the highest
    crawl priority tier.
    """
    return _matches_any(path, _OVERVIEW_PRIORITY_TYPES)


def looks_like_catalog_priority_url(path: str) -> bool:
    """Same idea as looks_like_overview_priority_url, one tier down: product
    pages, prioritized ahead of generic/undifferentiated pages (collections,
    blog) but behind Store Overview pages.
    """
    return _matches_any(path, _CATALOG_PRIORITY_TYPES)


def classify_page(url: str, path: str, is_homepage: bool, headings: list[str], body_text_sample: str) -> PageType:
    if is_homepage:
        return PageType.HOMEPAGE

    lowered_path = path.lower()
    for page_type, patterns in _URL_RULES:
        if any(re.search(p, lowered_path) for p in patterns):
            return page_type

    heading_text = " ".join(headings).lower()
    for page_type, patterns in _TEXT_RULES:
        if any(re.search(p, heading_text) for p in patterns):
            return page_type

    # A page with its own distinct heading that didn't match anything above
    # has already told us its identity - searching the rest of its body
    # prose for a stray one-off mention of a *different* policy topic (e.g.
    # a Legal Notice page's own sentence "for full details, see our privacy
    # policy", or a Payment Policy page's "...important billing details")
    # produces the same class of false positive as the earlier /my-account
    # bug: a page that merely references another policy gets classified AS
    # that policy. Found live against a real store (vellano.site): "Legal
    # Notice" -> misclassified privacy_policy, "Payment Policy" ->
    # misclassified checkout, both via a single cross-reference sentence
    # in otherwise-unrelated content. A wrong specific-type guess is worse
    # than an honest BLOG_OTHER here: on a store where no *other* page of
    # that type exists, the wrong tag would silently satisfy
    # check_required_pages and suppress a real "missing page" finding -
    # the same "confident conclusion from a signal that can't actually
    # support it" failure mode this project has repeatedly fixed elsewhere.
    # Only trust the body-wide fallback when there was no distinct heading
    # to begin with (nothing more specific to go on).
    if heading_text.strip():
        return PageType.BLOG_OTHER

    body_lower = body_text_sample.lower()
    for page_type, patterns in _TEXT_RULES:
        if any(re.search(p, body_lower) for p in patterns):
            return page_type

    return PageType.BLOG_OTHER
