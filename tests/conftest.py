import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("APP_PASSWORD", "test-password")

# The sandbox does not expose package installation. These tiny import stubs let
# pure SQL-generation/model tests run without opening a database connection.
if "psycopg" not in sys.modules:
    psycopg = types.ModuleType("psycopg")
    psycopg.Connection = object
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    types_pkg = types.ModuleType("psycopg.types")
    json_pkg = types.ModuleType("psycopg.types.json")
    class Jsonb:
        def __init__(self, value): self.value = value
    json_pkg.Jsonb = Jsonb
    sys.modules.update({
        "psycopg": psycopg,
        "psycopg.rows": rows,
        "psycopg.types": types_pkg,
        "psycopg.types.json": json_pkg,
    })
if "psycopg_pool" not in sys.modules:
    pool = types.ModuleType("psycopg_pool")
    pool.ConnectionPool = object
    sys.modules["psycopg_pool"] = pool
