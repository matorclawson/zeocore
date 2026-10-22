bump: minor

Add `zeo_core.integrations.agents`: a keyless wrapper over the managed OpenAI Agents
API (`client.beta.agents.sessions`, not the `openai-agents` SDK). Function actions
(`agent.session.requires_action`) are answered with `agent.session.input.tool_result`
carrying the matching `turn_id` and `call_id`; a turn succeeds only when
`agent.session.turn.completed` arrived and its output was inspected. Includes
`function_tool`, an optional `mcp_server` declaration, and a `ScriptedTransport` test
double. The SDK surface is refused as `agents_api_unavailable` when `openai` lacks it.
