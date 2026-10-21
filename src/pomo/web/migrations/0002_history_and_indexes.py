from __future__ import annotations

from django.db import migrations, models


def create_history_and_seed_current(apps, schema_editor) -> None:
    """Create the append-only audit table and seed the singleton pointer.

    The history table is not a Django model because it has no natural primary
    key and is written only by the raw-SQL store.
    """
    schema_editor.execute(
        "CREATE TABLE IF NOT EXISTS session_history ("
        "  id TEXT NOT NULL, state TEXT NOT NULL, start_epoch INTEGER NOT NULL,"
        "  duration INTEGER NOT NULL, origin_machine TEXT NOT NULL,"
        "  updated_at REAL NOT NULL, ended_at REAL, name TEXT, project TEXT, kind TEXT"
        ")"
    )
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS session_history_id_idx ON session_history (id)"
    )
    schema_editor.execute(
        "CREATE INDEX IF NOT EXISTS session_history_updated_at_idx ON session_history (updated_at)"
    )
    schema_editor.execute(
        "INSERT OR IGNORE INTO current (singleton, session_id, updated_at) VALUES (0, NULL, 0)"
    )


def _index(name: str, columns: str) -> migrations.SeparateDatabaseAndState:
    """Create an index idempotently while keeping Django's migration state.

    A legacy database may already have the index (for example if it was
    initialized by a newer store), so the database operation uses
    ``IF NOT EXISTS`` rather than Django's plain ``CREATE INDEX``.
    """
    return migrations.SeparateDatabaseAndState(
        database_operations=[
            migrations.RunSQL(
                f"CREATE INDEX IF NOT EXISTS {name} ON sessions ({columns})",
                reverse_sql=f"DROP INDEX IF EXISTS {name}",
            )
        ],
        state_operations=[
            migrations.AddIndex(
                model_name="session",
                index=models.Index(fields=columns.split(", "), name=name),
            )
        ],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("web", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_history_and_seed_current, migrations.RunPython.noop),
        _index("sessions_start_epoch_idx", "start_epoch"),
        _index("sessions_project_idx", "project"),
        _index("sessions_state_idx", "state"),
        _index("sessions_kind_state_idx", "kind, state"),
    ]
