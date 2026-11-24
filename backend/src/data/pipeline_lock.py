"""Process-wide lock serialising the heavy data pipelines.

Trailing endpoints acquire it non-blocking and answer {"status": "busy"} if
held; the current-year cycle, the sweep and refresh_teams acquire it
non-blocking and skip with a log line if held.
"""

import threading

PIPELINE_LOCK = threading.Lock()
