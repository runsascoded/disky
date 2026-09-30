"""`dt_cloud.secrets`: env + explicit secret values arrive whitespace-stripped."""
import pytest

from dt_cloud.secrets import env_secret, secret


def test_env_secret_strips(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("X_TOKEN", "abc\n")
    assert env_secret("X_TOKEN") == "abc"
    monkeypatch.setenv("X_TOKEN", " \tabc \r\n")
    assert env_secret("X_TOKEN") == "abc"
    monkeypatch.delenv("X_TOKEN")
    assert env_secret("X_TOKEN") is None
    assert env_secret("X_TOKEN", "d") == "d"


def test_secret_prefers_explicit_then_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("X_TOKEN", "env\n")
    assert secret("arg\n", "X_TOKEN") == "arg"
    assert secret(None, "X_TOKEN") == "env"
    assert secret("", "X_TOKEN") == "env"  # an empty option falls through to the env
    monkeypatch.delenv("X_TOKEN")
    assert secret(None, "X_TOKEN") is None
