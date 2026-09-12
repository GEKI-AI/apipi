from apipi.usage import add_usage, empty_usage, usage_from, usage_from_messages


def test_usage_from_maps_pi_and_drops_extras() -> None:
    usage = usage_from(
        {
            "input": 5,
            "output": 8,
            "cacheRead": 1,
            "cacheWrite": 2,
            "totalTokens": 16,
            "cost": {"total": 0.3},
            "prompt": "secret-prompt",
        }
    )
    assert usage == {
        "prompt_tokens": 5,
        "completion_tokens": 8,
        "cache_read_tokens": 1,
        "cache_write_tokens": 2,
        "total_tokens": 16,
    }
    assert "cost" not in usage
    assert "prompt" not in usage
    assert "secret-prompt" not in str(usage)


def test_missing_counts_are_zero() -> None:
    assert usage_from({}) == empty_usage()
    assert usage_from(None) == empty_usage()
    assert usage_from({"input": 4}) == {
        "prompt_tokens": 4,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def test_usage_from_messages_sums_assistant_only() -> None:
    usage = usage_from_messages(
        [
            {"role": "user", "content": "hi", "usage": {"input": 99}},
            {
                "role": "assistant",
                "usage": {"input": 1, "output": 2, "totalTokens": 3},
            },
            {
                "role": "toolResult",
                "usage": {"input": 7, "output": 8, "totalTokens": 15},
            },
            {
                "role": "assistant",
                "usage": {
                    "input": 4,
                    "output": 5,
                    "cacheRead": 1,
                    "cacheWrite": 2,
                    "totalTokens": 12,
                },
            },
        ]
    )
    assert usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "cache_read_tokens": 1,
        "cache_write_tokens": 2,
        "total_tokens": 15,
    }


def test_add_usage() -> None:
    other = empty_usage()
    other["prompt_tokens"] = 1
    assert add_usage(empty_usage(), other)["prompt_tokens"] == 1
