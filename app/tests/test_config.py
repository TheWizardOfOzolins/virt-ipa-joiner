import os
from app.config import load_config

def test_config_defaults():
    # clear env vars to test pure defaults
    os.environ.pop("IPA_HOST", None)
    conf = load_config("non_existent_file.yaml")
    assert conf["LOG_LEVEL"] == "INFO"
    assert "ubuntu" in conf["OS_MAP"]

def test_config_env_override(monkeypatch):
    # Set fake env var
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("IPA_HOST", "fake.ipa.com")
    
    conf = load_config("non_existent_file.yaml")
    
    assert conf["LOG_LEVEL"] == "DEBUG"
    assert conf["IPA_HOST"] == "fake.ipa.com"