"""Provider usage normalization and immutable, currency-specific price arithmetic."""

from decimal import Decimal, ROUND_CEILING

from oea.contracts.configuration import ModelPricing


def count(value):
    return value if type(value) is int and value >= 0 else None


def normalize(raw, profile="openai"):
    incoming = count(raw.get("prompt_tokens", raw.get("input_tokens")))
    outgoing = count(raw.get("completion_tokens", raw.get("output_tokens")))
    output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    reasoning = count(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    invalid = []
    if reasoning is not None and outgoing is not None and reasoning > outgoing:
        invalid.append("reasoning_exceeds_output")
        reasoning = None
    if profile == "anthropic":
        read = count(raw.get("cache_read_input_tokens"))
        write = count(raw.get("cache_creation_input_tokens"))
        uncached = incoming
        total = incoming + read + write if None not in (incoming, read, write) else None
    else:
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        read = count(details.get("cached_tokens", raw.get("cached_input_tokens", raw.get("prompt_cache_hit_tokens")))) if isinstance(details, dict) else None
        write = 0  # This profile has no separate cache-creation token category.
        total = incoming
        if read is not None and total is not None and read > total:
            invalid.append("cache_exceeds_input")
            read = None
        uncached = total - read if total is not None and read is not None else None
    return {"revision": "usage-v3.1", "profile": profile, "input_total": total, "input_uncached": uncached,
            "cache_read": read, "cache_write": write, "output_total": outgoing, "reasoning": reasoning,
            "reasoning_in_output": True, "source": "provider", "invalid_fields": invalid}


def micros(value):
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def upper_input_rate(price, profile):
    rates = [price.input_per_million]
    for name in (("cache_read_per_million", "cache_write_per_million") if profile == "anthropic" else ("cache_read_per_million",)):
        rate = getattr(price, name)
        rate = price.cache_upper_bound_per_million if rate is None else rate
        if rate is None:
            return None
        rates.append(rate)
    return max(rates)


def charge(normalized, price, *, estimated_input, estimated_output):
    if price is None:
        return None, "unknown"
    price = ModelPricing.model_validate(price)
    components = (("input_uncached", "input_per_million"), ("cache_read", "cache_read_per_million"),
                  ("cache_write", "cache_write_per_million"), ("output_total", "output_per_million"))
    total, estimated = Decimal(0), False
    for dimension, rate_name in components:
        tokens = normalized.get(dimension)
        if tokens == 0:
            continue
        rate = getattr(price, rate_name)
        if rate is None and dimension.startswith("cache_"):
            rate = price.cache_upper_bound_per_million
            estimated = True
        if tokens is None or rate is None:
            upper = upper_input_rate(price, normalized["profile"])
            if upper is None:
                return None, "unknown"
            incoming = normalized.get("input_total")
            outgoing = normalized.get("output_total")
            total = upper * (estimated_input if incoming is None else incoming) + price.output_per_million * (estimated_output if outgoing is None else outgoing)
            return int(total.to_integral_value(rounding=ROUND_CEILING)), "estimated"
        total += tokens * rate
    return int(total.to_integral_value(rounding=ROUND_CEILING)), "estimated" if estimated else "settled"
