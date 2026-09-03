"""Part 3 (broader real-world crawl robustness round): the classifier's URL
and text rules were English-only, which meant a fully localized store (both
URL slugs and content in another language) fell through to BLOG_OTHER -
producing a false "missing" finding downstream even though the page exists.
Confirmed live against a real translated WooCommerce store before this fix
(see PROJECT_PROGRESS notes for this round). This covers the handful of
languages this round added real coverage for; full multilingual support
remains an explicit, scoped-out follow-up (see app.checks.deterministic's
language-aware downgrade for what happens outside this coverage).
"""
from app.models import PageType
from app.page_classifier import classify_page


def test_french_privacy_policy_localized_slug_and_text():
    result = classify_page(
        "https://boutique.example/politique-de-confidentialite",
        "/politique-de-confidentialite", is_homepage=False,
        headings=["Politique de confidentialité"],
        body_text_sample="Cette politique de confidentialité décrit les données personnelles que nous collectons.",
    )
    assert result == PageType.PRIVACY_POLICY


def test_spanish_shipping_policy_localized_slug_and_text():
    result = classify_page(
        "https://tienda.example/politica-de-envio", "/politica-de-envio", is_homepage=False,
        headings=["Política de envío"],
        body_text_sample="Nuestra política de envío detalla los tiempos de entrega y gastos de envío.",
    )
    assert result == PageType.SHIPPING_POLICY


def test_german_returns_policy_localized_slug_and_text():
    result = classify_page(
        "https://shop.example/rueckgabe", "/rueckgabe", is_homepage=False,
        headings=["Widerrufsrecht"],
        body_text_sample="Unser Widerrufsrecht und unsere Erstattungsrichtlinie im Detail.",
    )
    assert result == PageType.RETURNS_POLICY


def test_portuguese_terms_of_service_text_only():
    result = classify_page(
        "https://loja.example/legal", "/legal", is_homepage=False,
        headings=["Termos e Condições"],
        body_text_sample="Estes termos e condições regem o uso desta loja.",
    )
    assert result == PageType.TERMS_OF_SERVICE


def test_italian_contact_page_localized_slug():
    result = classify_page(
        "https://negozio.example/chi-siamo", "/chi-siamo", is_homepage=False,
        headings=["Chi siamo"], body_text_sample="Contattaci per qualsiasi domanda.",
    )
    assert result == PageType.CONTACT_ABOUT


def test_french_woocommerce_product_category_slug_is_classified_as_collection():
    """Confirmed live against a real French WooCommerce store (meo.fr):
    WooCommerce's own French-locale default category slug is
    /categorie-produit/, not /product-category/ - falling through to
    BLOG_OTHER meant these pages got none of the catalog-priority crawl
    seeding or product-specific checks that PageType.COLLECTION/PRODUCT get."""
    result = classify_page(
        "https://boutique.example/categorie-produit/cafe-en-grain",
        "/categorie-produit/cafe-en-grain", is_homepage=False, headings=[], body_text_sample="",
    )
    assert result == PageType.COLLECTION


def test_french_woocommerce_product_slug_is_classified_as_product():
    result = classify_page(
        "https://boutique.example/produit/cafe-arabica", "/produit/cafe-arabica", is_homepage=False,
        headings=[], body_text_sample="",
    )
    assert result == PageType.PRODUCT


def test_unsupported_language_still_falls_through_to_blog_other():
    """A language this round doesn't have pattern coverage for (e.g.
    Japanese) correctly still can't be classified - the honesty fix lives in
    app.checks.deterministic (downgrade to CANNOT_VERIFY, not a silent
    misclassification here)."""
    result = classify_page(
        "https://shop.example/プライバシーポリシー",
        "/プライバシーポリシー", is_homepage=False,
        headings=["プライバシーポリシー"],
        body_text_sample="このプライバシーポリシーでは、当社が収集する個人情報について説明します。",
    )
    assert result == PageType.BLOG_OTHER
