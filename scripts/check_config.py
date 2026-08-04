from app.config import get_settings
from app.db import close_pool, connection

if __name__ == "__main__":
    settings = get_settings()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(),current_user,to_regclass('public.rd_bars'),to_regclass('public.ra_jobs')")
            print(cur.fetchone())
        conn.rollback()
    print("Default password still set:", settings.app_password == "change-me")
    close_pool()
