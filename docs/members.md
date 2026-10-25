# Members and keys

An organization has owners and members. Owners register providers, add and change harnesses,
manage members and see every session; members run the harnesses and see their own sessions.

## Members

`hg members`, or the API. Three ways to add one:

- **Invite by email** (`POST /v1/invites`, `hg members invite EMAIL --role member`): when that
  email signs up or signs in, it becomes a member with the role you chose. With sign-up closed, an
  invitation is the only way a new account can be created.
- **Add a name for a key** (`POST /v1/members`, `hg members add NAME --role member`): a member
  without an account, for a service or a script; mint it a key.
- **Sign-up**, when open: the account's own organization, with the account as owner.

`PATCH /v1/members/{name}` changes a role; `DELETE` removes the member and revokes their keys. An
organization keeps at least one owner.

## Keys

A key (`hg_…`) opens the organization as its member from any OpenAI SDK or `hg`. Mint
one per place it is used and revoke the one that leaks. `hg keys new --label laptop`; owners mint
for any member (`--member NAME`). A key is shown once. `GET /v1/keys` lists them by label, member
and creation; `DELETE /v1/keys/{id}` revokes.

## Reach

Reach is organization, then member: a member sees their own sessions and responses, an owner the
organization's, nobody another organization's; every miss is a 404. A key names one member; an
account switches between its organizations and acts as its member in each.
