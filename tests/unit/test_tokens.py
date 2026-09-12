from apipi.tokens import generate_token, hash_token, token_matches


def test_token_matches_uses_digest() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert len(digest) == 64
    assert token_matches(token, digest)
    assert not token_matches("nope", digest)
    assert not token_matches(token, "0" * 64)


def test_generate_token_unique() -> None:
    assert generate_token() != generate_token()
