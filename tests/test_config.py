from __future__ import annotations

import pytest

from assistant.core.config import ConfigService
from assistant.core.security import SecretBox, hash_password, redact, verify_password


def test_layering_yaml_then_env_then_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "timezone: Europe/Oslo\n"
        "llm:\n  chat:\n    model: from-yaml\n    base_url: http://yaml/v1\n    temperature: 0.5\n"
    )
    env = {
        "PA_LLM__CHAT__MODEL": "from-env",
        "PA_LLM__CHAT__API_KEY": "12345",  # stays a string
        "PA_LLM__MAX_RETRIES": "7",
        "PA_SECRET_KEY": "not-a-config-path",
        "PA_SERVER__PORT": "",  # empty values are ignored
        "UNRELATED": "x",
    }
    service = ConfigService.load(path, env)
    cfg = service.config
    assert cfg.timezone == "Europe/Oslo"
    assert cfg.llm.chat.model == "from-env"
    assert cfg.llm.chat.base_url == "http://yaml/v1"
    assert cfg.llm.chat.api_key == "12345"
    assert cfg.llm.max_retries == 7
    assert cfg.server.port == 8000

    service.apply_overrides({"llm.chat.model": "from-ui", "llm.chat.fallback.model": "backup"})
    assert service.config.llm.chat.model == "from-ui"
    assert service.config.llm.chat.fallback.model == "backup"
    assert service.config.llm.chat.temperature == 0.5

    service.apply_overrides({})  # removing overrides reverts to file/env values
    assert service.config.llm.chat.model == "from-env"


def test_invalid_values_and_protected_paths_are_rejected():
    service = ConfigService()
    with pytest.raises(ValueError):
        service.apply_overrides({"timezone": "Mars/Olympus"})
    with pytest.raises(ValueError):
        service.apply_overrides({"database.url": "sqlite:///elsewhere.db"})
    with pytest.raises(ValueError):
        ConfigService({"llm": {"chat": {"modle": "typo"}}})
    assert service.config.timezone == "UTC"  # unchanged after failures


def test_slots_inherit_endpoint_from_chat():
    llm = ConfigService(
        {
            "llm": {
                "chat": {"base_url": "http://a/v1", "api_key": "k", "model": "chat-m"},
                "embeddings": {"model": "embed-m"},
                "stt": {"base_url": "http://whisper/v1", "model": "whisper-1"},
            }
        }
    ).config.llm
    emb = llm.resolve("embeddings")
    assert (emb.base_url, emb.api_key, emb.model) == ("http://a/v1", "k", "embed-m")
    # A different server does not get the chat key.
    assert llm.resolve("stt").api_key is None
    assert llm.resolve("vision").model == "chat-m"
    assert llm.resolve("tts") is None and llm.resolve("fallback") is None


def test_secret_box_round_trip_and_key_file(tmp_path):
    box = SecretBox.from_env_or_file(tmp_path, env_value="")
    token = box.encrypt("refresh-token")
    assert "refresh-token" not in token
    key_file = tmp_path / "secret.key"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert SecretBox.from_env_or_file(tmp_path, env_value="").decrypt(token) == "refresh-token"
    with pytest.raises(ValueError):
        SecretBox.from_env_or_file(tmp_path, env_value="other-key").decrypt(token)


def test_password_hashing_and_redaction():
    hashed = hash_password("correct horse")
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("x", "not-a-hash")

    line = redact('POST /v1 Authorization: Bearer abcdef123456 api_key="sk-live-abcdef1234567890"')
    assert "abcdef123456" not in line and "sk-live" not in line
