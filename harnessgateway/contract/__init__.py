"""Layer 1, the contract: what a harness package is (`package`), what crosses the wire in both
directions (`wire`), and the runner that executes a turn inside a sandbox (`turn.sh`). A package
wraps any command-line program: the platform hands it the client's Responses request, verbatim, and
takes back Responses events; nothing here knows what the program does."""
