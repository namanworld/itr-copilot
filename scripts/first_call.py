from decimal import Decimal

from anthropic import Anthropic

from itr.settings import get_settings

MILLION = Decimal(1_000_000)


def main() -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY in .env first")

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": "In one sentence, what is Form 16 in Indian Income Tax?",
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            print(block.text)

    usage = response.usage
    print(usage.model_dump_json(indent=2))

    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0

    cost = (
        usage.input_tokens * settings.price_input_per_mtok
        + usage.output_tokens * settings.price_output_per_mtok
        + cache_write * settings.price_cache_write_per_mtok
        + cache_read * settings.price_cache_read_per_mtok
    ) / MILLION
    print(f"Model: {response.model}  Cost: ${cost:.6f}")


if __name__ == "__main__":
    main()
