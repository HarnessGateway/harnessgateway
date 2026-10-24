# The hg command line

`hg` is the command line for a member of an organization. It talks to the platform's HTTP API
with an API key, and runs a harness package on your machine while you develop one. It installs
with the platform (`pip install harnessgateway` or `uv tool install harnessgateway`).

```
hg login URL KEY                 remember where the platform is and who you are (~/.config/hg/config.json)
hg me                            who you are, your organization, your teams
hg providers [put NAME --url U --api A --key K | rm NAME]
hg catalog [NAME]                the shipped packages
hg harnesses                     the harnesses you can use
hg harness new NAME --catalog P | --fork H | --dir DIR [--team T]
hg harness pull NAME DIR         the package's files into DIR
hg harness push NAME DIR [-m MSG]   DIR as the next commit (a new harness when none)
hg harness log NAME | rm NAME | show NAME
hg harness run DIR [--input TEXT | --request FILE] [--state DIR] [--env K=V] [--install] [-v]
                                 run request.sh and response.sh here, as the platform would, and
                                 print the response; no sandbox, your own environment and keys
hg ask MODEL TEXT [--stream] [--previous ID] [--file PATH]...   POST /v1/responses
hg response ID [--detail | --request]
hg sessions | hg session ID [--files | --cat PATH | --trace | --archive OUT]
hg steer SESSION TEXT | hg cancel RESPONSE
hg rodeo TASK MODEL MODEL... [--pick HARNESS] | hg rodeo --list | hg pick ID HARNESS
hg keys | hg keys new [--label L] [--member M]
hg teams | hg teams put NAME MEMBER... [--title T] | hg teams rm NAME
hg members | hg members add NAME [--role R] | hg members invite EMAIL [--role R]
```

`HG_URL` and `HG_KEY` in the environment override the saved login. Output is JSON, except `ask
--stream`, which prints the text as it arrives and the items as they end.

## A first session

```bash
hg login https://hg.example.com hg_…
hg providers put deepseek --url https://api.deepseek.com/anthropic --api anthropic-messages --key sk-…
hg harness new claude-code --catalog claude-code
hg ask claude-code "Create hello.py that prints hello and run it" --stream
hg ask claude-code "Now make it print it twice" --previous resp_…
hg session sess_… --files
```
