"""Reference USD per 1,000 tokens (standard text input/output; verify before paid use).
The default Gemini and Groq aliases are explicitly priced as free-tier usage only.
Cached-input, batch, tax, regional and higher-context pricing are not included.
"""
from decimal import Decimal

# model: (provider, input USD / 1K, output USD / 1K)
MODELS = {
    'gemini-2.0-flash': ('gemini', Decimal('0'), Decimal('0')),
    'gemini-2.0-flash-paid': ('gemini', Decimal('0.00010'), Decimal('0.00040')),
    'llama-3.3-70b-versatile': ('groq', Decimal('0'), Decimal('0')),
    'llama-3.3-70b-versatile-paid': ('groq', Decimal('0.00059'), Decimal('0.00079')),
    'gpt-4o-mini': ('openai', Decimal('0.00015'), Decimal('0.00060')),
    'claude-3-5-haiku-20241022': ('anthropic', Decimal('0.0008'), Decimal('0.0040')),
    'claude-3-5-haiku-latest': ('anthropic', Decimal('0.0008'), Decimal('0.0040')),
}

def cost_usd(model, prompt_tokens, completion_tokens):
    _, input_rate, output_rate = MODELS[model]
    return (Decimal(prompt_tokens) * input_rate + Decimal(completion_tokens) * output_rate) / Decimal(1000)
