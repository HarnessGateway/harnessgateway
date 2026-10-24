# The console

The console at `/app` is a plain web page over the same routes any client uses; nothing it does
is unavailable to the [API](api.md) or the [command line](cli.md). It is organized the way a new
organization proceeds.

| Section | Pages | What for |
|---|---|---|
| Setup | Providers, Harnesses, Catalog | the endpoints and keys; the organization's packages with an editor; the shipped packages to fork |
| Build | Conversations, Rodeo, Sessions | talk to a harness with its workspace beside it; compare harnesses; every session with its trace |
| Team | Teams, Members, Keys, Account | who is in the organization and what they own |

The deployer's key opens a different console: Dashboard (usage, counts only), Organizations
(create, suspend, limits) and Workers (the fleet).

<figure class="shot" markdown>
![The catalog](assets/console/catalog.png)
<figcaption>The catalog: each card shows the CLI's version, the default provider and the verification checks.</figcaption>
</figure>

<figure class="shot" markdown>
![The package editor](assets/console/editor.png)
<figcaption>A harness: its files, an editor, the commit history, fork, export and delete.</figcaption>
</figure>

<figure class="shot" markdown>
![A conversation](assets/console/conversation.png)
<figcaption>A conversation: the items the harness produced (commands, patches, reasoning, messages) and the workspace pane with files, trace and log.</figcaption>
</figure>

<figure class="shot" markdown>
![A rodeo](assets/console/rodeo.png)
<figcaption>A rodeo: one task on several harnesses side by side, a winner picked, the leaderboard.</figcaption>
</figure>

<figure class="shot" markdown>
![The deployer's dashboard](assets/console/dashboard.png)
<figcaption>The deployer's dashboard: turns, sandbox time, provider calls and bytes relayed, by day, organization and harness.</figcaption>
</figure>
