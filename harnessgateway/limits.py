"""Every fixed bound the platform enforces, in one place. Per-organization ceilings live in `domain.orglimits`;
these are the hard limits no organization can raise."""
from __future__ import annotations

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3

# HTTP
REQUEST_BODY_MAX = 32 * MB              # a Responses request, inline files included
FILE_UPLOAD_MAX = 512 * MB              # one /v1/files or container file upload
PACKAGE_UPLOAD_MAX = 64 * MB            # one harness package archive
CONTROL_BODY_MAX = 1 * MB               # every other JSON body
NODE_BODY_MAX = 16 * MB                 # a node's event batch or finish report

# Packages
PACKAGE_FILES_MAX = 5_000
PACKAGE_BYTES_MAX = 256 * MB            # unpacked

# Turns
EVENT_LINE_MAX = 1 * MB                 # one stdout line from request.sh
TURN_STDOUT_MAX = 64 * MB               # everything a turn prints
STDERR_TAIL = 64 * KB                   # what is kept of stderr without observability
INPUT_FILES_MAX = 64                    # file_id references materialized per turn
SNAPSHOT_TIMEOUT_S = 600

# Gateway
CAPTURE_BODY_MAX = 4 * MB               # per request or response body kept in an observability capture

# Lists
PAGE_MAX = 100
