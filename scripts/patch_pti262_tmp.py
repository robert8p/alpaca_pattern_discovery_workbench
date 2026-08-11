from pathlib import Path

p=Path('app/db.py'); s=p.read_text()
s=s.replace('def _apply_v261_point_in_time_hotfix(cur: Any) -> None:\n    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.1.sql").read_text(encoding="utf-8"))',
'''def _apply_v262_point_in_time_availability_migration(cur: Any) -> None:
    cur.execute((Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.2.sql").read_text(encoding="utf-8"))''')
s=s.replace('_apply_v261_point_in_time_hotfix(cur)','_apply_v262_point_in_time_availability_migration(cur)')
p.write_text(s)

p=Path('scripts/release_audit.py'); s=p.read_text()
s=s.replace('migration_pti_hotfix = (ROOT / "sql/migrations/2.6.1.sql").read_text(encoding="utf-8")','migration_pti_hotfix = (ROOT / "sql/migrations/2.6.2.sql").read_text(encoding="utf-8")')
s=s.replace('_apply_v261_point_in_time_hotfix(cur)','_apply_v262_point_in_time_availability_migration(cur)')
p.write_text(s)

p=Path('tests/test_schema_startup.py'); s=p.read_text().replace('2.6.1.sql','2.6.2.sql').replace('_apply_v261_point_in_time_hotfix','_apply_v262_point_in_time_availability_migration'); p.write_text(s)
