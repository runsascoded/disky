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


class FakeResponse:
    def __init__(self, data: str):
        from base64 import b64encode

        self.body = {"payload": {"data": b64encode(data.encode()).decode()}}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.body


class FakeSession:
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.urls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        resource = url.removeprefix("https://secretmanager.googleapis.com/v1/").removesuffix(":access")
        return FakeResponse(self.values[resource])


def test_parse_binding():
    from dt_cloud.secrets import parse_binding

    assert parse_binding("A=tok", "p") == ("A", "projects/p/secrets/tok/versions/latest")
    assert parse_binding("A=tok@3", "p") == ("A", "projects/p/secrets/tok/versions/3")
    assert parse_binding("A=projects/q/secrets/t", "p") == ("A", "projects/q/secrets/t/versions/latest")
    assert parse_binding("A=projects/q/secrets/t/versions/2", "p") == ("A", "projects/q/secrets/t/versions/2")
    for bad in ["A", "=tok", "A="]:
        with pytest.raises(ValueError):
            parse_binding(bad, "p")


def test_resolve_fetches_unset_and_keeps_set():
    from dt_cloud.secrets import resolve

    s = FakeSession({"projects/p/secrets/a/versions/latest": "va\n", "projects/p/secrets/c/versions/latest": " vc "})
    env = {"B": "injected", "C": ""}
    assert resolve(["A=a", "B=b", "C=c"], "p", s, env) == {"A": "va", "C": "vc"}
    assert s.urls == [
        "https://secretmanager.googleapis.com/v1/projects/p/secrets/a/versions/latest:access",
        "https://secretmanager.googleapis.com/v1/projects/p/secrets/c/versions/latest:access",
    ]
