# HarnessGateway, in brief

Five questions, and the shortest whole answer to each. [SPEC.md](../SPEC.md) is the long form, and
[ARCHITECTURE.md](ARCHITECTURE.md) says how the parts work together.

## What is the goal?

Anyone can build an agent against the unmodified OpenAI Responses API and, by changing only `model` to
`environment/harness/provider/model`, have any harness CLI answer in a sandbox as OpenAI's own model would, because
a harness package builder that understands the schema, the platform, and the developer on the other end can write a
correct package for any CLI.

## Why is a session the boundary?

A session's whole state is two folders: the mind, a home shared by every harness CLI that works on the session, and
the work. Whoever keeps that state can hand it to any `environment/harness/provider/model` for the next turn:
either the platform, which saves and restores the folders by `previous_response_id`, or the developer, who resends
the conversation so a new mind and work begin from it. Client side and server side are only where the state is
kept.

## How does the platform work, from request to response?

A caller sends a Responses request with an API key, and the platform takes it through six steps.

1. **Accept.** It authenticates the caller and checks the body against the OpenAI SDK's own types. It resolves
   `model` into an environment, a harness, and a provider, and checks any `previous_response_id` and uploaded
   files. It swaps the request's credentials for placeholders, lets an edition refuse the request, and queues a
   turn whose first event is `response.created`.
2. **Claim.** A runner claims the turn under a row lock and keeps a heartbeat.
3. **Prepare.** The runner installs the harness package once for that image. It restores the session's mind and
   work from the parent's snapshot, or starts them empty. It writes the turn's inputs: the request as sent, the
   previous response, the handover history, the uploaded files, and the startup script. It binds every secret to a
   placeholder and records, under a one-time token, what this sandbox may reach.
4. **Run.** It starts one hardened container that runs the startup script and then the package's `run`. `run` reads
   the request, drives its harness CLI, and prints output items and stream events. The platform checks each line
   against the SDK, numbers it, and stores it, so any process can stream it, hold the connection for the final
   response, or answer later for a background request.
5. **Guard.** All outbound traffic goes through the egress proxy, which enforces the environment's network policy
   and puts real secrets in only for the hosts they are bound to. The platform meters CPU, memory, network, and
   disk, and stops the sandbox on cancel, timeout, disk limit, or an invalid line.
6. **Finish.** Unless the request said `store: false`, it saves the mind and work as a snapshot of deduplicated
   chunks. It decides the outcome, with what it observed outranking what the package said, and writes the final
   response and the stream's last event. It then removes the turn's inputs and its grant, and forgets an unstored
   turn entirely.

## Where is state kept, with `store` true or false?

With `store: true`, the default, state lives on the server. The platform snapshots `/mind` (the home where each
harness CLI keeps its own session files) and `/work` after every turn, and restores them when the next request
names `previous_response_id`. The CLI resumes its own memory, which is the natural fit, because harness CLIs
remember by writing files in their home.

With `store: false`, state lives with the developer. The turn runs in a throwaway session, starting empty or from
the named response's snapshot. The CLI still uses a mind and a work folder during the turn, but nothing is saved
and both are removed after the answer. So the developer resends the whole conversation each time, and the CLI
starts fresh from that text, with none of its own memory and none of the files it made.

## How does the harness package builder build any harness package?

It researches the CLI by the seven-step framework and answers the five primitive questions for it: input, context,
model interface, output, and control flow. From that, with the Responses schemas, the platform's facts, a whole
example package, and a session of real developer requests in hand, it writes a `README.md`, an `install`, and a
`run` that turn any Responses request into that CLI's command and its output into Responses items. It adds optional
files only for what was asked beyond the CLI as it comes, and tests the package against that session before
handing it over.
