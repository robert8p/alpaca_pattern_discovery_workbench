import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql://user:pass@localhost:5432/test"),
)
os.environ.setdefault("APP_PASSWORD", "test-password")

# Use the real driver whenever it is installed. Earlier releases checked only
# sys.modules and therefore replaced an installed Psycopg with a stub before it
# had a chance to import, invalidating driver-level SQL tests.
try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
except ImportError:
    psycopg = types.ModuleType("psycopg")
    psycopg.Connection = object
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    errors = types.ModuleType("psycopg.errors")

    class ReadOnlySqlTransaction(Exception):
        pass

    errors.ReadOnlySqlTransaction = ReadOnlySqlTransaction
    types_pkg = types.ModuleType("psycopg.types")
    json_pkg = types.ModuleType("psycopg.types.json")

    class Jsonb:
        def __init__(self, value):
            self.value = value

    json_pkg.Jsonb = Jsonb
    sys.modules.update(
        {
            "psycopg": psycopg,
            "psycopg.rows": rows,
            "psycopg.errors": errors,
            "psycopg.types": types_pkg,
            "psycopg.types.json": json_pkg,
        }
    )
    pool = types.ModuleType("psycopg_pool")
    pool.ConnectionPool = object
    sys.modules["psycopg_pool"] = pool
