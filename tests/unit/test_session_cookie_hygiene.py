from flask import Flask, session

from app.core.setup import request_hooks


def _large_session_value() -> str:
    return "".join(f"{idx:04x}-{idx * 7919:08x};" for idx in range(1000))


def test_session_cookie_guard_drops_noncritical_data_before_save(app):
    app.config["SESSION_COOKIE_SOFT_LIMIT_BYTES"] = 600

    with app.test_request_context("/workflow/10017/update_status"):
        session["_user_id"] = "123"
        session["_fresh"] = True
        session["_id"] = "login-session-id"
        session["csrf_token"] = "csrf-token"
        session["next"] = "/case/example"
        session["large_blob"] = _large_session_value()
        session["_flashes"] = [("success", "Text Text.")]

        request_hooks._shrink_session_cookie_if_needed(app, session)

        assert session["_user_id"] == "123"
        assert session["_fresh"] is True
        assert session["_id"] == "login-session-id"
        assert session["csrf_token"] == "csrf-token"
        assert session["next"] == "/case/example"
        assert "large_blob" not in session
        assert session["_flashes"] == [("success", "Text Text.")]
        assert request_hooks._session_cookie_payload_size(app, session) <= 600


def test_session_cookie_guard_caps_flash_messages(app):
    app.config["SESSION_COOKIE_SOFT_LIMIT_BYTES"] = 10000

    with app.test_request_context("/case/example"):
        session["_flashes"] = [
            ("warning", f"message-{idx}-" + ("x" * 500)) for idx in range(7)
        ]

        request_hooks._shrink_session_cookie_if_needed(app, session)

        flashes = session["_flashes"]
        assert len(flashes) == 5
        assert flashes[0][1].startswith("message-2-")
        assert all(len(message) <= 303 for _category, message in flashes)


def test_response_header_guard_resaves_single_shrunk_session_cookie():
    local_app = Flask(__name__)
    local_app.secret_key = "test-secret-key"
    local_app.config["SESSION_COOKIE_NAME"] = "new_ipm_session"
    local_app.config["RESPONSE_HEADER_SOFT_LIMIT_BYTES"] = 700
    original_save_session = local_app.session_interface.save_session

    with local_app.test_request_context("/workflow/10017/update_status"):
        session["_user_id"] = "123"
        session["_fresh"] = True
        session["csrf_token"] = "csrf-token"
        session["next"] = "/case/example"
        session["temporary_blob"] = _large_session_value()
        session["_flashes"] = [("success", "Text Text.")]

        response = local_app.response_class("ok")
        response.headers["Content-Security-Policy"] = "default-src 'self'; " + ("a" * 500)
        original_save_session(local_app, session, response)
        before_size = request_hooks._response_header_size(response)

        request_hooks._resave_shrunk_session_for_response_headers(
            local_app,
            session,
            response,
            original_save_session,
        )

        after_size = request_hooks._response_header_size(response)
        set_cookies = response.headers.getlist("Set-Cookie")

        assert before_size is not None
        assert after_size is not None
        assert after_size <= before_size
        assert len(
            [
                value
                for value in set_cookies
                if value.startswith("new_ipm_session=")
            ]
        ) == 1
        assert "temporary_blob" not in session
        assert "_flashes" not in session
        assert session["next"] == "/case/example"
