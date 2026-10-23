import litellm

import config


def main() -> None:
    resp = litellm.completion(
        model=config.LLM_MODEL,
        reasoning_effort=config.LLM_REASONING_EFFORT,
        messages=[{"role": "user", "content": "Reply with: ok"}],
    )
    print(resp.choices[0].message.content)


if __name__ == "__main__":
    main()
