"""The bearer guard fails CLOSED, and the deploy is why that matters.

Run:  python3 -m pytest tests/test_auth_fails_closed.py -q

deploy/DEPLOY_EC2.md instructs opening TCP 8080 to 0.0.0.0/0 and justifies it
with "it's protected by a bearer token below". Before 2026-09-07 the guard read

    if _API_TOKEN and authorization != f"Bearer {_API_TOKEN}":
        raise HTTPException(401)

so an UNSET OTTOQ_API_TOKEN short-circuited the whole comparison and every
request was served. A missing environment variable and a correct deployment
looked identical from outside, and the sentence in the deploy doc quietly
became false.

Each test below fails if the guard is reverted to the `if _API_TOKEN and ...`
form: the first two because an unconfigured service would answer instead of
refusing, the third because a wrong token would be accepted.
"""

import importlib
import os

import pytest
from fastapi import HTTPException


def _guard(**env):
    """Reload app.main under a given environment and hand back require_token.

    The module reads both variables at import time, so the reload is the point:
    it is what lets one test process exercise several deployment postures.
    """
    keys = ("OTTOQ_API_TOKEN", "OTTOQ_ALLOW_UNAUTHENTICATED")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        import app.main as m
        importlib.reload(m)
        return m.require_token
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_unconfigured_service_refuses_every_request():
    """No token configured -> 503, including for a caller that sends nothing."""
    guard = _guard()
    with pytest.raises(HTTPException) as e:
        guard(authorization="")
    assert e.value.status_code == 503
    assert "OTTOQ_API_TOKEN" in e.value.detail


def test_unconfigured_service_refuses_a_caller_that_guesses_a_token():
    """A bearer header must not make an unconfigured service answer."""
    guard = _guard()
    with pytest.raises(HTTPException) as e:
        guard(authorization="Bearer anything-at-all")
    assert e.value.status_code == 503


def test_configured_service_rejects_a_wrong_token_and_accepts_the_right_one():
    guard = _guard(OTTOQ_API_TOKEN="the-real-token")
    with pytest.raises(HTTPException) as e:
        guard(authorization="Bearer not-the-real-token")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        guard(authorization="")
    assert guard(authorization="Bearer the-real-token") is None


def test_local_development_opts_out_explicitly():
    """The one way to run without auth is a variable somebody had to type."""
    guard = _guard(OTTOQ_ALLOW_UNAUTHENTICATED="1")
    assert guard(authorization="") is None
    #: and it is opt-IN: any other value keeps the service closed
    guard = _guard(OTTOQ_ALLOW_UNAUTHENTICATED="true")
    with pytest.raises(HTTPException) as e:
        guard(authorization="")
    assert e.value.status_code == 503
