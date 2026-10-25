# Sessions

A session is one conversation with one harness in one workspace: the chain of turns that share a
workspace, the CLI's state, and the home the member's package keeps. A request without
`previous_response_id` starts one; each continuation adds a turn. A session runs one turn at a
time.

## One turn at a time, the next message wins

A request with `previous_response_id` naming a session that is running a turn **supersedes** that
turn: the platform stops it gracefully (the package's stop signal, `SIGTERM` to `request.sh`'s
process group, then `stop_timeout_s` of grace, then `response.sh` if there is one), the stopped
response ends `cancelled` with `metadata.incomplete_reason: superseded` and whatever it produced,
and the new turn is queued behind it: it runs when the stopped one has ended, on the workspace and
state the stopped one left, as turn n+1. The trace records the supersede. A turn still queued when
the next request arrives ends at once, with no response. This is what `hg ask --previous` does
from a second shell while the first is still running, and what a client does when its user types
the next message; there is no separate steer or interrupt call.

## Cancel, and closing the request

A turn belongs to the request that made it. Close that request, by closing the stream or by giving
up on a blocking call, and the turn is stopped the same way: no endpoint to call, nothing left
running for nobody. The response ends `cancelled` with `metadata.incomplete_reason:
client_disconnected` and the items produced so far, and the session is idle again. `Ctrl-C` on
`hg ask` does this.

A turn made with `background: true` was asked to outlive its request: following it
(`GET /v1/responses/{id}?stream=true`) and closing the stream changes nothing. It ends when the
package ends, on `POST /v1/responses/{id}/cancel` (`hg cancel RESPONSE`), or when a new request
on the session supersedes it. Cancel stops a turn on purpose; the response ends `cancelled` with
`metadata.incomplete_reason: cancelled`.

## The workspace

`GET /v1/sessions/{id}/files` lists the workspace after the last turn, `…/files/{path}` reads one
(4 MB at most through the API), `…/archive` downloads the workspace as a tar. `hg session ID
--files`, `--cat PATH`, `--archive OUT`. Workspaces are snapshotted after every turn and restored
before the next, so a session can continue on any node; the snapshot counts against the
organization's `disk_mb` limit.

## The trace

`GET /v1/sessions/{id}/trace` is the session's record: for each turn the request as the client
sent it (file contents replaced by their sizes), the response as returned, and every cancel,
disconnect and supersede with its time. `GET /v1/responses/{id}/captures` lists the provider calls
the gateway relayed for that turn, and `…/captures/{name}` returns one exchange, request and
response bodies included (the key is never in it).

## Listing and deleting

`GET /v1/sessions` lists every session the member can see: their own, and for owners the whole
organization's, with the harness, the member, the status (`running` or `idle`), the turns and
whether a workspace is kept. `DELETE /v1/sessions/{id}` removes one with everything it holds; a
running one cannot be deleted.

## Stateless turns

`store: false` runs the turn and keeps the response but no workspace and no state; it cannot be
continued. For one-shot tasks that should leave nothing behind.
