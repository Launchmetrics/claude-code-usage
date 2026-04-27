import summarizer


def test_prompt_hash_is_deterministic():
    assert summarizer.prompt_hash("hello") == summarizer.prompt_hash("hello")


def test_prompt_hash_differs_on_change():
    assert summarizer.prompt_hash("hello") != summarizer.prompt_hash("hello!")


def test_prompt_hash_returns_hex_string():
    h = summarizer.prompt_hash("hello")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex digest length
    int(h, 16)  # valid hex


def test_prompt_hash_handles_unicode():
    summarizer.prompt_hash("hola — què tal?")  # must not raise
