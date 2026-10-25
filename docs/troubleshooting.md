# Troubleshooting

**The first boot's keys are gone from the log.** `harnessgateway init` (or `docker exec
harnessgateway harnessgateway init`) makes a new deployer key; `init --org NAME` a new
organization with its owner's key. For an existing organization, mint a key from the console as
an owner, or as the deployer `PATCH` the organization and invite a new owner.

**`no provider: name one in the request's model path`.** The package has no default provider and
the request named none. Pick one in the app's Provider dropdown, or use `harness/provider` as
the model.

**A turn fails with `package_output_invalid`.** The package printed a stream event the SDK cannot
parse; the log at `GET /v1/responses/{id}/detail` names the event and the field. Run the package
with `hg harness run` to see its raw output.

**`install.sh` fails.** `GET /v1/responses/{id}/detail` has its output. Most often a host missing
from `install.egress`: the door refused it, and the door's log in the detail says which. Pin the
CLI's version; registries move.

**The turn hangs, then times out.** The CLI waited for a TTY or an approval. Every catalog package
runs its CLI in a headless, auto-approving mode; a package of your own needs the same flags. The
sandbox has no TTY.

**The CLI cannot reach the model.** It called a host other than `$HG_PROVIDER_URL`. Point it at the
provider_env names (the catalog packages write the CLI's config each turn with that URL), and make
sure the provider's `api` shape matches what the CLI speaks: an Anthropic-shaped CLI on an
`openai-chat` provider gets 404s from the gateway, which only forwards the shape's own paths.

**`private address refused`.** The egress list names a host that resolves to a private address.
On a self-hosted model server that is intended: set `HG_ALLOW_PRIVATE=1` on the workers.

**Sessions are not continuing where they left off.** The package must keep the CLI's resume handle
in `$HG_STATE` and use it when `HG_PREVIOUS` is set; the catalog's packages do. A CLI whose
headless mode cannot resume (Cline 3.0, OpenClaw's exec) is marked `recall: does not pass` in the
catalog, and the package says what it does instead.

**A worker shows `lost`.** It stopped heartbeating: the process died, or it cannot reach
`HG_CONTROL_URL`. Its turns failed with that message; start it again and it rejoins with its
credential. `DELETE /v1/workers/{id}` forgets one for good.

**On a Mac, sandboxes cannot see the data.** They can: the platform inspects its own container and
translates the mounts. If you run the API outside Docker (`uv run harnessgateway serve`) and the
daemon in Docker Desktop, that translation does not apply; use `--dev` for development, or run the
image.

**The app shows an old version.** It is served by the API; restart after an upgrade. The
browser caches nothing across versions.
