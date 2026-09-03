from app.report import safe_host_for_filename


def test_strips_port_colon_that_windows_would_treat_as_ads_separator():
    assert safe_host_for_filename("http://localhost:8917") == "localhost-8917"


def test_plain_domain_unaffected():
    assert safe_host_for_filename("https://example.com") == "example.com"


def test_adds_scheme_when_missing():
    assert safe_host_for_filename("example.com") == "example.com"


def test_www_subdomain_preserved():
    assert safe_host_for_filename("https://www.example.com") == "www.example.com"
