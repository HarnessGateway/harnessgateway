"""The node role: runs sandboxes on one machine. It joins the control plane with a join token, then pulls turns, runs
each in its session's gVisor sandbox, streams the package's output back, checkpoints the workspace and reports the
compute and network it used. It holds a node credential and nothing else: no database, bucket or provider key."""
