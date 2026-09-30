from mirea_lecture_assistant.logging_setup import redact


def test_redacts_email_uuid_and_secret_fields():
    source = (
        "user student@gmail.com token=12345678 "
        "qr 550e8400-e29b-41d4-a716-446655440000 password: hunter2 "
        "https://sso.example/login?session_code=private&execution=also-private"
    )
    result = redact(source)
    assert "student@gmail.com" not in result
    assert "550e8400-e29b-41d4-a716-446655440000" not in result
    assert "12345678" not in result
    assert "hunter2" not in result
    assert "private" not in result
    assert "also-private" not in result
    assert "<email>" in result
    assert "<uuid>" in result
    assert result.count("<hidden>") == 4


def test_redacts_json_tokens_and_jwts():
    source = (
        '{"access_token":"eyJheader123456.payload123456.signature123"} '
        '{"refresh_token":"plain-private-value"}'
    )

    result = redact(source)

    assert "payload123456" not in result
    assert "plain-private-value" not in result


def test_headers_cookies_fragments_codes_and_any_uuid_are_hidden():
    source = (
        "Authorization: Bearer abc.def.ghi\n"
        "Cookie: KEYCLOAK_IDENTITY=secret-cookie; other=1\n"
        "https://sso.mirea.ru/cb#state=frag-secret&code=frag-code\n"
        "emailCode=654321 код: 123456 {access_token: plainvalue}\n"
        "token 01890a5d-ac96-774b-bcce-b302099a8057"
    )
    result = redact(source)
    for secret in (
        "abc.def.ghi",
        "secret-cookie",
        "frag-secret",
        "frag-code",
        "654321",
        "123456",
        "plainvalue",
        "01890a5d-ac96-774b-bcce-b302099a8057",
    ):
        assert secret not in result, secret
