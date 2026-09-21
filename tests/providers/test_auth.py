from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiden.providers.auth import (
    Credential,
    env_var_names,
    import_pi,
    load_auth_file,
    require,
    resolve,
    save_auth_file,
    status,
)
from aiden.providers.errors import AuthError


def test_env_var_map_covers_requested_providers():
    assert env_var_names("anthropic")[0] == "ANTHROPIC_API_KEY"
    assert env_var_names("deepseek") == ("DEEPSEEK_API_KEY",)
    assert env_var_names("openai") == ("OPENAI_API_KEY",)
    # both opencode flavours share one key, matching pi
    assert env_var_names("opencode") == env_var_names("opencode-go") == ("OPENCODE_API_KEY",)
    # unknown providers derive a name rather than failing
    assert env_var_names("my-proxy") == ("MY_PROXY_API_KEY",)


def test_explicit_api_key_wins():
    cred = resolve("anthropic", api_key="sk-test", env={"ANTHROPIC_API_KEY": "sk-env"})
    assert cred is not None and cred.key == "sk-test"
    assert cred.source == "argument"


def test_env_used_when_no_argument():
    cred = resolve("deepseek", env={"DEEPSEEK_API_KEY": "sk-deep"})
    assert cred is not None and cred.key == "sk-deep"
    assert cred.source == "env:DEEPSEEK_API_KEY"


def test_anthropic_oauth_env_ordering():
    cred = resolve("anthropic", env={"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat-x"})
    assert cred is not None and cred.source == "env:ANTHROPIC_AUTH_TOKEN"


def test_returns_none_when_unauthenticated(tmp_path: Path):
    assert resolve("anthropic", env={}, path=tmp_path / "missing.json") is None


def test_auth_file_api_key(tmp_path: Path):
    path = tmp_path / "auth.json"
    save_auth_file({"openai": {"type": "api_key", "key": "sk-file"}}, path)
    cred = resolve("openai", env={}, path=path)
    assert cred is not None and cred.key == "sk-file" and cred.type == "api_key"


def test_auth_file_oauth_shape(tmp_path: Path):
    path = tmp_path / "auth.json"
    save_auth_file(
        {
            "commandcode": {
                "type": "oauth",
                "access": "user_abc",
                "refresh": "user_abc",
                "expires": 4_102_444_800_000,
            }
        },
        path,
    )
    cred = resolve("commandcode", env={}, path=path)
    assert cred is not None
    assert cred.type == "oauth"
    assert cred.key == "user_abc"
    assert not cred.expired


def test_expired_oauth_flagged(tmp_path: Path):
    path = tmp_path / "auth.json"
    save_auth_file({"commandcode": {"type": "oauth", "access": "user_abc", "expires": 1}}, path)
    cred = resolve("commandcode", env={}, path=path)
    assert cred is not None and cred.expired


def test_key_never_fully_displayed():
    cred = Credential(provider="anthropic", type="api_key", key="sk-ant-1234567890")
    assert "1234567890" not in cred.display
    assert cred.display.startswith("sk-a")


def test_require_raises_auth_error_with_guidance(tmp_path: Path):
    with pytest.raises(AuthError) as exc:
        require("deepseek", env={}, path=tmp_path / "none.json")
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_save_sets_owner_only_permissions(tmp_path: Path):
    path = save_auth_file({"x": {"type": "api_key", "key": "k"}}, tmp_path / "auth.json")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_corrupt_auth_file_is_treated_as_empty(tmp_path: Path):
    path = tmp_path / "auth.json"
    path.write_text("{not json")
    assert load_auth_file(path) == {}


def test_import_from_pi_fixture(tmp_path: Path):
    pi_auth = tmp_path / "pi-auth.json"
    pi_auth.write_text(
        json.dumps(
            {
                "anthropic": {"type": "api_key", "key": "sk-ant-1"},
                "commandcode": {
                    "type": "oauth",
                    "access": "user_1",
                    "refresh": "user_1",
                    "expires": 9,
                },
                "google": {"type": "api_key", "key": "gm-1"},
            }
        )
    )
    target = tmp_path / "aiden-auth.json"

    written = import_pi(pi_path=pi_auth, target=target)
    assert set(written) == {"anthropic", "commandcode", "google"}

    data = load_auth_file(target)
    assert data["anthropic"] == {"type": "api_key", "key": "sk-ant-1"}
    assert data["commandcode"]["access"] == "user_1"

    # idempotent without --overwrite
    assert import_pi(pi_path=pi_auth, target=target) == []
    # filtered import
    assert import_pi(pi_path=pi_auth, target=target, providers=["anthropic"], overwrite=True) == [
        "anthropic"
    ]


def test_status_rows_hide_secrets(tmp_path: Path):
    path = tmp_path / "auth.json"
    save_auth_file({"openai": {"type": "api_key", "key": "sk-secret-value"}}, path)
    rows = status(["openai", "deepseek"], path)
    assert rows[0]["authenticated"] is True
    assert "secret-value" not in rows[0]["key"]
    assert rows[1]["authenticated"] is False
