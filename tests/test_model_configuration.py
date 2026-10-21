"""Provider selection and SDK payload checks without transport or credentials."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from baseagent.llm import Model, ModelResponse

cli = importlib.import_module("baseagent.main")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for prefix in ("BASEAGENT_LLM_", "DEEPSEEK_"):
        for name in ("API_KEY", "BASE_URL", "MODEL", "PROVIDER", "EXTRA_BODY"):
            monkeypatch.delenv(prefix + name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://old.invalid")


def test_deepseek_fallback_and_default(monkeypatch):
    assert cli.model_configuration() == {"api_key": "old-key", "base_url": "https://old.invalid", "model_name": "deepseek-chat"}
    monkeypatch.setenv("DEEPSEEK_MODEL", "old-model")
    assert cli.model_configuration()["model_name"] == "old-model"
    assert cli.model_configuration("explicit")["model_name"] == "explicit"
    with pytest.raises(ValueError):
        cli.model_configuration(generic_only=True)


def test_generic_precedence_and_extra_body(monkeypatch):
    monkeypatch.setenv("BASEAGENT_LLM_API_KEY", "new-key")
    with pytest.raises(ValueError):
        cli.model_configuration()  # Never mix the new key with the old endpoint.
    monkeypatch.setenv("BASEAGENT_LLM_BASE_URL", "https://compatible.invalid/v1")
    monkeypatch.setenv("BASEAGENT_LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("BASEAGENT_LLM_EXTRA_BODY", '{"enable_thinking":false}')
    factory = Mock()
    monkeypatch.setattr(cli, "Model", factory)
    cli.initialize_model(timeout=12)
    assert factory.call_args.kwargs == {"api_key": "new-key", "base_url": "https://compatible.invalid/v1",
                                       "model_name": "qwen-plus", "extra_body": {"enable_thinking": False}, "timeout": 12}
    assert cli.model_configuration("explicit")["model_name"] == "explicit"


@pytest.mark.parametrize("body", ["[]", "null", "oops", '{"x":NaN}'])
def test_invalid_extra_body_rejected_before_client(monkeypatch, body):
    monkeypatch.setenv("BASEAGENT_LLM_API_KEY", "key")
    monkeypatch.setenv("BASEAGENT_LLM_BASE_URL", "https://compatible.invalid/v1")
    monkeypatch.setenv("BASEAGENT_LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("BASEAGENT_LLM_EXTRA_BODY", body)
    with pytest.raises(ValueError, match="JSON object"):
        cli.initialize_model()


@pytest.mark.parametrize("extra", [None, {"enable_thinking": False}])
def test_sdk_extra_body_and_reasoning_roundtrip(monkeypatch, extra):
    message = {"role": "assistant", "content": "ok"}
    sdk = Mock()
    sdk.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=2)))
    monkeypatch.setattr("baseagent.llm.model.OpenAI", Mock(return_value=sdk))
    # HTTP client is fake too: this test does not allocate a transport.
    monkeypatch.setattr("baseagent.llm.model.DefaultHttpxClient", Mock())
    with Model("qwen-plus", "fake-key", "https://compatible.invalid/v1", extra_body=extra) as model:
        response = model.complete([])
    kwargs = sdk.chat.completions.create.call_args.kwargs
    assert ("extra_body" in kwargs) is (extra is not None)
    if extra is not None:
        assert kwargs["extra_body"] == extra
    assert ModelResponse.from_dict(response.to_dict()).reasoning_tokens == 2
