"""Migration registry. Import all migration modules to register them."""
from inferfabric.db import IFFDB

# v001: state table
from inferfabric.migrations import v001_init_state
IFFDB.register_migration(1, "state", "init_state")

# v002: request_log table
from inferfabric.migrations import v002_init_request_log
IFFDB.register_migration(2, "request_log", "init_request_log")

# v003: manual_stops table (v5.2)
from inferfabric.migrations import v003_manual_stops
IFFDB.register_migration(3, "state", "manual_stops")

# v004: sleep_state table (v5.2)
from inferfabric.migrations import v004_sleep_state
IFFDB.register_migration(4, "state", "sleep_state")
