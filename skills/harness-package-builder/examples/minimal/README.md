# minimal

One streamed model call per turn, the conversation kept in `$HG_STATE`. Copy this directory to start a package.

| Request field | Mapping |
|---|---|
| `input` text | the user message (`Request.prompt`) |
| `instructions` | appended to the system prompt, after the `PERSONA` setting and the workspace's `AGENTS.md` |
| `previous_response_id` | the platform continues the session; the history in `$HG_STATE` is the conversation |
| `model` | `$HG_MODEL` |
| everything else | ignored |

Output: the reply streamed as `response.output_text.delta` events, usage on `response.completed`, provider errors as
`response.failed`.
