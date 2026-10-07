from mirea_lecture_assistant.logging_setup import redact


def test_encoded_urls_and_secret_aliases_are_redacted_after_formatting():
    import logging
    from urllib.parse import quote

    from mirea_lecture_assistant.logging_setup import RedactingFormatter

    sources = [
        'code=synthetic-secret client_secret=synthetic-secret session_state=synthetic-secret',
        '{"code":"synthetic-secret", "client_secret":"synthetic-secret"}',
        'https://sso.mirea.ru/callback?code=synthetic-secret&state=synthetic-secret',
    ]
    for source in sources:
        for _ in range(3):
            record = logging.LogRecord("test", logging.WARNING, __file__, 1,
                                       "failure %r", (source,), None)
            assert "synthetic-secret" not in RedactingFormatter().format(record)
            source = quote(source, safe="")
    assert redact("pulse_auth_step status=401 path=/api/mireaauth") == (
        "pulse_auth_step status=401 path=/api/mireaauth"
    )


def test_secret_value_delimiters_numeric_json_and_escaped_quotes():
    import logging

    from mirea_lecture_assistant.logging_setup import RedactingFormatter

    sources = [
        'https://sso.mirea.ru/cb?code=prefix%26synthetic-secret',
        'https://sso.mirea.ru/cb?code=prefix%23synthetic-secret',
        'https://sso.mirea.ru/cb?code=prefix%20synthetic-secret',
        'password=prefix&synthetic-secret',
        '{"code":123456,"otp":123456}',
        '{"password":"prefix\\"synthetic-secret"}',
    ]
    for source in sources:
        exc = RuntimeError(source)
        record = logging.LogRecord("test", logging.WARNING, __file__, 1, "failed", (),
                                   (RuntimeError, exc, None))
        result = RedactingFormatter().format(record)
        assert "synthetic-secret" not in result
        assert "123456" not in result
    assert redact("status_code=401 error_code=invalid_grant") == (
        "status_code=401 error_code=invalid_grant"
    )


def test_oidc_cookie_names_hide_nonce_and_correlation_suffixes():
    result = redact(
        ".AspNetCore.Correlation.synthetic-secret "
        ".AspNetCore.OpenIdConnect.Nonce.synthetic-secret Pulse.Auth.Cookie"
    )
    assert "synthetic-secret" not in result
    assert "Pulse.Auth.Cookie" in result


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
