from app.llm.policy_chunker import chunk_html_by_headings


def test_splits_at_heading_boundaries():
    html = """
    <html><body><main>
        <h2>Return window</h2>
        <p>Customers may return items within 30 days of delivery.</p>
        <h2>Refund method</h2>
        <p>Refunds are issued to the original payment method within 5 business days.</p>
    </main></body></html>
    """
    chunks = chunk_html_by_headings(html)
    sections = [section for section, _ in chunks]
    assert "Return window" in sections
    assert "Refund method" in sections
    return_chunk = next(text for section, text in chunks if section == "Return window")
    assert "30 days" in return_chunk
    refund_chunk = next(text for section, text in chunks if section == "Refund method")
    assert "5 business days" in refund_chunk


def test_content_before_first_heading_becomes_introduction():
    html = (
        "<html><body><main>"
        "<p>Overview text with no heading yet, long enough to clear the minimum chunk size.</p>"
        "<h2>Details</h2><p>More text that is also long enough to clear the minimum chunk size threshold.</p>"
        "</main></body></html>"
    )
    chunks = chunk_html_by_headings(html)
    assert chunks[0][0] == "Introduction"
    assert "Overview text" in chunks[0][1]


def test_drops_near_empty_fragments():
    html = (
        "<html><body><main>"
        "<h2>Empty Section</h2>"
        "<h2>Real Section</h2><p>This section has real, substantial content describing an actual policy requirement in detail.</p>"
        "</main></body></html>"
    )
    chunks = chunk_html_by_headings(html)
    sections = [section for section, _ in chunks]
    assert "Empty Section" not in sections
    assert "Real Section" in sections


def test_long_section_is_split_on_sentence_boundaries_not_mid_sentence():
    sentence = "This is a policy sentence that repeats to build up length. "
    long_text = sentence * 30  # well over the target chunk size
    html = f"<html><body><main><h2>Long Section</h2><p>{long_text}</p></main></body></html>"
    chunks = chunk_html_by_headings(html, target_chars=200)
    long_section_chunks = [text for section, text in chunks if section == "Long Section"]
    assert len(long_section_chunks) > 1
    for piece in long_section_chunks:
        stripped = piece.strip()
        assert stripped.endswith(".") or stripped == long_section_chunks[-1].strip()


def test_removes_nav_header_footer_boilerplate():
    html = (
        "<html><body>"
        "<nav>Home | About | Contact</nav>"
        "<main><h2>Policy</h2><p>The real policy text describing a specific merchant requirement in enough detail.</p></main>"
        "<footer>Copyright 2026</footer>"
        "</body></html>"
    )
    chunks = chunk_html_by_headings(html)
    all_text = " ".join(text for _, text in chunks)
    assert "Home | About" not in all_text
    assert "Copyright" not in all_text
    assert "real policy text" in all_text
