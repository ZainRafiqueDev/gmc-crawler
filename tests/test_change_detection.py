from app.change_detection import compute_content_hash, compute_dom_hash


def test_content_hash_stable_across_whitespace_differences():
    a = compute_content_hash("Hello   world\n\ntest")
    b = compute_content_hash("Hello world test")
    assert a == b


def test_content_hash_changes_when_text_changes():
    a = compute_content_hash("Returns accepted within 30 days.")
    b = compute_content_hash("Returns accepted within 60 days.")
    assert a != b


def test_dom_hash_stable_across_text_only_changes():
    a = compute_dom_hash("<html><body><p>Hello</p></body></html>")
    b = compute_dom_hash("<html><body><p>Goodbye</p></body></html>")
    assert a == b


def test_dom_hash_changes_when_structure_changes():
    a = compute_dom_hash("<html><body><p>Hello</p></body></html>")
    b = compute_dom_hash("<html><body><p>Hello</p><div>New section</div></body></html>")
    assert a != b


def test_hashes_handle_none_and_empty_gracefully():
    assert compute_content_hash(None) == compute_content_hash("")
    assert compute_dom_hash(None) == compute_dom_hash("")
