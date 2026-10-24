# Rodeo

A rodeo runs one task across several harnesses at once and keeps the winner you pick. The picks
accumulate into a leaderboard for your organization, on your tasks, with your providers: the
comparison that matters, rather than a public ranking on someone else's.

## Run one

Rodeo in the console: write the task, tick two to six harnesses, choose a provider when the
packages have no default, Run. Each harness runs in a session of its own and streams into its
column; when all have ended, "Pick the winner" records the matchup with your pick.

```bash
hg rodeo "Create hello.txt containing hello, then say done" claude-code codex pi --pick codex
hg rodeo --list
hg pick RODEO_ID codex
```

Over the API a rodeo is made from responses you already have: `POST /v1/rodeos` with
`{"task": "…", "entries": [{"response": "resp_…"}, …]}`, then `PATCH /v1/rodeos/{id}` with
`{"winner": "harness"}`. `GET /v1/rodeos` lists them with the leaderboard (wins and runs per
harness).

<figure class="shot" markdown>
![A rodeo](assets/console/rodeo.png)
</figure>

Each column is a normal session: open it from Sessions to read its workspace and trace, or continue
it. A rodeo's sessions count against the organization's turns-at-once limit together, so six
harnesses need `max_runs` of six to run truly side by side; with fewer they queue.
