# API reference

Base URL: `https://<platform>/v1`. Authentication: `Authorization: Bearer sk-hg-...` (an organization's API key).
Errors use the OpenAI envelope: `{"error": {"message", "type", "param", "code"}}` with 400, 401, 403, 404, 409, 413,
429 or 503. Any OpenAI SDK works unchanged: `OpenAI(base_url="https://<platform>/v1", api_key="sk-hg-...")`.

## OpenAI-compatible

| Method and path | Notes |
|---|---|
| `POST /responses` | Run a turn. `model` is `harness`, `harness/provider` or `harness/provider/model`. `stream: true` returns SSE; `background: true` returns immediately; otherwise the request waits for the end. `previous_response_id` continues the session. `store: false` discards the session after the turn. Header `Idempotency-Key` makes retries safe. A foreground turn is cancelled if the client disconnects. |
| `GET /responses/{id}` | The response. `?stream=true&starting_after=N` replays events after sequence number N, then follows live. |
| `POST /responses/{id}/cancel` | Stops a queued or running turn (SIGTERM, then SIGKILL after the package's grace). |
| `DELETE /responses/{id}` | Deletes a finished response and its stored request. |
| `GET /responses/{id}/input_items` | The request's input items. |
| `POST /files` · `GET /files` · `GET /files/{id}` · `GET /files/{id}/content` · `DELETE /files/{id}` | Input files; reference them in `input` as `{"type": "input_file", "file_id": ...}`. The package finds them in `$HG_FILES`. |
| `GET /containers` · `GET /containers/{id}` · `DELETE /containers/{id}` | Sessions. A container is created by a session's first response; its id is `response.metadata.session_id`. |
| `GET /containers/{id}/files` · `GET .../files/{fid}` · `GET .../files/{fid}/content` | The workspace after the last turn (paths under `/workspace`). |
| `POST /containers/{id}/files` (multipart `file`, optional `path`) · `DELETE .../files/{fid}` | Staged into the workspace when the session's next turn starts. |
| `GET /models` · `GET /models/{id}` | The harness/provider combinations the organization can call. |

Every response carries `metadata.session_id`. When the platform (not the package) ended a turn, `metadata.hg_reason`
says why: `cancelled`, `client_disconnected`, `timeout`, `no_terminal_event`, `invalid_output`, `output_too_large`,
`install_failed`, `sandbox_failed`, `node_lost`, `workspace_too_large`.

## The organization's resources

| Method and path | Who | Notes |
|---|---|---|
| `POST /signup` | anyone with an invite code | `{"invite_code", "organization", "email"}` → the organization and its first admin key (shown once). |
| `GET /organization` | any key | The organization, its limits, and the caller's member id and role. |
| `GET /keys` · `POST /keys` · `DELETE /keys/{id}` | admin | `POST {"name", "role": "admin"\|"member", "member_id"?, "ttl_days"?}` returns the secret once. |
| `GET /members` · `POST /members` · `DELETE /members/{id}` | admin to change | `POST {"email", "role"}`. |
| `GET /providers` · `PUT /providers/{name}` · `GET` · `DELETE` | admin to change | `PUT {"api": "anthropic"\|"openai", "base_url", "key"}`. The base URL is the one before `/v1`. The key is encrypted at once and never returned. |
| `GET /catalog` | any key | The platform's built-in harnesses. |
| `GET /harnesses` · `GET /harnesses/{name}` | any key | Installed harnesses: version, digest, provider shapes, settings (set or not, never values). |
| `POST /harnesses` | admin | `{"builtin": name}`, `{"git": {"url", "ref", "path"}}` (any public repository: the free marketplace), or multipart `package` (a .tar.gz of the package). Re-installing upgrades; running sessions keep their version. |
| `PATCH /harnesses/{name}` | admin | `{"settings": {"NAME": "value"\|null}, "default_provider", "default_model"}`. |
| `DELETE /harnesses/{name}` | admin | |
| `GET /usage?start=&end=` | any key | Metered usage and charges for the window (Unix seconds; default: this month). |
| `GET /traces/{response_id}` | any key, observability on | The request the package received, every event, full stdout/stderr, exit code, provider exchanges. |

## Operator (`/admin/v1`, `Authorization: Bearer $HG_OPERATOR_KEY`)

`POST /invites {"label", "uses", "ttl_days", "limits"}` · `POST /organizations {"name", "owner_email", "limits"}` ·
`GET /organizations` · `GET|PATCH /organizations/{id}` (`{"limits": {...}, "status": "active"|"suspended"}`) ·
`GET /organizations/{id}/usage` · `POST /node-tokens {"org_id"?, "label", "uses", "ttl_days"}` · `GET /nodes` ·
`POST /nodes/{id}/drain` · `DELETE /nodes/{id}` · `GET /prices`.

Organization limits (all optional; defaults in parentheses): `vcpus` (2), `memory_mb` (4096), `pids` (1024),
`max_sandboxes` (10), `max_running_turns` (10), `turn_timeout_s` (3600), `idle_ttl_s` (300),
`session_retention_days` (30), `workspace_mb` (4096), `storage_gb` (50), `requests_per_minute` (600),
`monthly_charge_usd` (0 = no cap), `observability` (false), `dedicated_nodes` (false).

## Nodes (`/internal/v1`)

Used by the node role only: `POST /nodes/join` (join token), then with the node credential `POST /nodes/heartbeat`,
`POST /jobs/claim`, `POST /turns/{id}/events`, `POST /turns/{id}/finish`, `POST /usage`, and signed
`GET|PUT /blobs/{token}`.

Health: `GET /healthz` (process up), `GET /readyz` (database and storage reachable).
