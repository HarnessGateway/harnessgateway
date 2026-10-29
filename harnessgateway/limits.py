"""The platform's fixed bounds (SPEC §17). They bound what a caller may send and what a process holds in memory; none
of them limits what a harness does."""
from __future__ import annotations

KB, MB = 1024, 1024 ** 2

REQUEST_BODY_MAX = 32 * MB          # a Responses request, inline files included
FILE_UPLOAD_MAX = 512 * MB          # one /v1/files or container file upload
PACKAGE_UPLOAD_MAX = 64 * MB        # one package archive
PACKAGE_BYTES_MAX = 256 * MB        # unpacked
PACKAGE_FILES_MAX = 5_000
CONTROL_BODY_MAX = 1 * MB           # any other JSON body
INPUT_FILES_MAX = 64                # file_id references per request
EVENT_LINE_MAX = 64 * MB            # one stdout line from a package
EVENT_BATCH_BYTES = 1 * MB          # the node sends stdout lines in batches of about this size
NODE_EVENTS_BODY_MAX = EVENT_LINE_MAX + EVENT_BATCH_BYTES + 64 * KB
NODE_BODY_MAX = 8 * MB              # any other node report
CAPTURE_BODY_MAX = 4 * MB           # a captured request or response body, each side
LOG_TAIL = 16 * KB                  # what a turn keeps of its log lines and stderr without full observability
SETTINGS_MAX = 64                   # settings, and secrets, per harness
SETTING_VALUE_MAX = 16 * KB
EGRESS_MAX = 256                    # an org's egress entries
PERSIST_MAX = 64                    # a manifest's persist paths
PAGE_MAX = 100
