from bot.crypto import TokenVault
from bot.utils import clean_catalog_text, generate_obfuscated_post_token, post_id_from_token


def test_username_cleanup():
    value = "Operating Systems @BookGram\nhttps://t.me/hello Complete Notes @example_123"
    cleaned = clean_catalog_text(value)
    assert "@BookGram" not in cleaned
    assert "t.me/hello" not in cleaned
    assert "@example_123" not in cleaned
    assert "Operating Systems" in cleaned


def test_token_contains_only_post_digits_as_digits():
    token = generate_obfuscated_post_token(256)
    assert post_id_from_token(token) == 256
    assert "".join(ch for ch in token if ch.isdigit()) == "256"


def test_bot_token_encryption_roundtrip():
    vault = TokenVault("test-secret")
    token = "123456:ABCDEF"
    encrypted = vault.encrypt(token)
    assert encrypted != token
    assert vault.decrypt(encrypted) == token
