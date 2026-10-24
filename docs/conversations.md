# Conversations and sessions

A session is one conversation with one harness in one workspace: the chain of turns that share a
workspace, the CLI's state, and the home the member's package keeps. A request without
`previous_response_id` starts one; each continuation adds a turn.

## In the console

Conversations: pick a harness (and a provider and model when the package has no default or you
want another), write the task, Send. The items stream in as the harness produces them: commands
with their output, patches, reasoning, messages. The pane on the right shows the workspace after
each turn (every file, readable), the trace (every request and response as sent, every steer and
cancel) and the log (the package's own lines and stderr). The session list on the left reopens any
conversation, including one still running on another client; the stream resumes from its start.

<figure class="shot" markdown>
![A conversation](assets/console/conversation.png)
</figure>

## Steering

While a turn runs, the text box steers it: Steer sends the text to the session, the platform
writes it to `$HG_DIR/interrupt` in the sandbox and sends `request.sh` the package's steer signal
(`interrupt` in `harness.json`, `SIGUSR1` by default). What happens next is the package's: the
catalog packages let the running turn finish and run the steer's text as a second turn of the same
CLI session, so the steer lands with everything the first turn did. Over the API it is
`POST /v1/sessions/{id}/interrupt` with `{"input": "…"}`; `hg steer SESSION TEXT` on the command
line. A CLI with no way to take a message mid-run gets the steer as the next turn's input, which
the package documents.

## Cancel

Cancel stops the turn (`POST /v1/responses/{id}/cancel`, `hg cancel RESPONSE`): `SIGTERM` to the
package's process group, the stop grace from `harness.json`, then `response.sh` if there is one.
The response ends `cancelled` with the items produced so far; the session is idle again and can
continue.

## The workspace

`GET /v1/sessions/{id}/files` lists the workspace after the last turn, `…/files/{path}` reads one
(4 MB at most through the API), `…/archive` downloads workspace and state as a tar. `hg session
ID --files`, `--cat PATH`, `--archive OUT`. Workspaces are snapshotted after every turn and
restored before the next, so a session can continue on any worker; the snapshot counts against
the organization's `disk_mb` limit.

## The trace

`GET /v1/sessions/{id}/trace` is the session's record: for each turn the request as the client
sent it (file contents replaced by their sizes), the response as returned, and every steer and
cancel with its time. `GET /v1/responses/{id}/captures` lists the provider calls the gateway
relayed for that turn, and `…/captures/{name}` returns one exchange, request and response bodies
included (the key is never in it). The console's trace tab shows the first; the second is for
when a CLI's behaviour needs the wire itself.

## Sessions

Sessions lists every session the member can see: their own, and for owners the whole
organization's, with the harness, the member, the status, the turn count and whether a workspace
is kept. A session is deleted with everything it holds (`DELETE /v1/sessions/{id}`); a running
one cannot be.

## Stateless turns

`store: false` runs the turn and keeps the response but no workspace and no state; it cannot be
continued. For one-shot tasks that should leave nothing behind.
