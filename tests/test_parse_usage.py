import pytest

from opencode_tracker.logger import parse_stream_chunks, parse_usage


OPENAI = {
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "cost": 0.00412,
        "opencode": {
            "billing_source": "balance",
            "list_cost": 0.005,
            "estimated": False,
            "tokens": {
                "input": 1200,
                "output": 300,
                "reasoning": 120,
                "cache_read": 8000,
                "cache_write_5m": 10,
                "cache_write_1h": 20,
            },
        },
    }
}


def test_parse_openai_usage():
    parsed = parse_usage(OPENAI)
    assert parsed["cost"] == pytest.approx(0.00412)
    assert parsed["billing_source"] == "balance"
    assert parsed["list_cost"] == pytest.approx(0.005)
    assert parsed["prompt_tokens"] == 1200
    assert parsed["completion_tokens"] == 300
    assert parsed["tokens"]["cache_read"] == 8000
    assert parsed["raw_location"] == "usage"


def test_parse_anthropic_usage():
    parsed = parse_usage({
        "usage": {
            "input_tokens": 900,
            "output_tokens": 100,
            "cost": 0.0,
            "opencode": {
                "billing_source": "go",
                "allowance": 0.42,
                "tokens": {"input": 900, "output": 100},
            },
        }
    })
    assert parsed["billing_source"] == "go"
    assert parsed["allowance"] == pytest.approx(0.42)
    assert parsed["prompt_tokens"] == 900
    assert parsed["completion_tokens"] == 100


def test_parse_gemini_usage_metadata():
    parsed = parse_usage({
        "usageMetadata": {
            "promptTokenCount": 500,
            "candidatesTokenCount": 50,
            "cost": 0.001,
            "opencode": {"billing_source": "free", "tokens": {"input": 500, "output": 50}},
        }
    })
    assert parsed["raw_location"] == "usageMetadata"
    assert parsed["billing_source"] == "free"
    assert parsed["prompt_tokens"] == 500
    assert parsed["completion_tokens"] == 50


def test_missing_usage_returns_empty_fields():
    parsed = parse_usage({"error": "nope"})
    assert parsed["cost"] is None
    assert parsed["tokens"] == {}
    assert parsed["raw_location"] is None


def test_stream_uses_final_usage_chunk():
    chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [], "usage": OPENAI["usage"]},
    ]
    parsed = parse_stream_chunks(chunks)
    assert parsed["billing_source"] == "balance"
    assert parsed["cost"] == pytest.approx(0.00412)


def test_stream_handles_responses_completed():
    chunks = [
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.completed", "response": OPENAI},
    ]
    parsed = parse_stream_chunks(chunks)
    assert parsed["cost"] == pytest.approx(0.00412)
