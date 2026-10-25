bump: minor

Add Moonshot (Kimi) as an `llms` provider: `get_llm_client("moonshot")` uses the OpenAI-compatible client at `https://api.moonshot.ai/v1` with the default model `kimi-k3.1-pro`. The key comes from `MOONSHOT_API_KEY` and is never written to `OPENAI_API_KEY`; OpenAI's organisation and project identifiers are not sent to Moonshot; the sampling parameters Moonshot fixes for K3 are left out and `max_tokens` is sent as `max_completion_tokens`. Plain and streaming chat only: tools, functions and tool messages are refused, a reply that is empty, cut at the token limit or ended as a tool call is an error rather than an empty success, errors say Moonshot, the base URL must be https, and the managed `LLMIntegration` service does not discover the provider yet. `OpenAIClient` behaves as before.

sow: zeocore-SOW-44
