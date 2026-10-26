"""The api role: HTTP in, HTTP out. Routers are thin: authenticate, read the body (after authentication, within its
limit), call one service, shape the reply. OpenAI-compatible routes live under /v1 with OpenAI's names; the platform's
own resources use names OpenAI does not; the operator's routes are under /admin/v1 and the nodes' under /internal/v1."""
