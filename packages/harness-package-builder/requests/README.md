# The requests a developer sends

One session of an application built on the Responses API, request by request: a consultant's engagement with a
client, as the example package's agent would be sent it. A package is tested with them, in order.

| File | What the developer does | What a package must get right |
|---|---|---|
| `turn-1.json` | Starts an engagement, with instructions and a tool of their own | The harness acts by itself, and says so |
| `turn-2.json` | Continues, with a picture of two colours and a file | Its own memory, the image, the file |
| `turn-3.json` | Asks for the client's budget, which only their tool can answer | The response ends, completed, on the call |
| `turn-4.json` | Sends the tool's result | It carries on from the answer |
| `turn-5.json` | Changes the instructions, and asks for an answer that fits a schema | Instructions for that response only; the schema, or a failure that names it |
| `turn-6.json` | Keeps the conversation themselves, and sends all of it | A new session with nothing but the request to go on |

`previous_response_id` and `call_id` name what the response before returned: here `resp_turn_<n>` and
`call_turn_<n>_1` stand for them.
