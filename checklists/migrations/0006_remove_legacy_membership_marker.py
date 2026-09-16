from django.db import migrations


LEGACY_COLUMN = "is_test_staff"


def remove_legacy_membership_column(apps, schema_editor):
    membership = apps.get_model("checklists", "ProgramMembership")
    table_name = membership._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor, table_name
            )
        }
    if LEGACY_COLUMN not in columns:
        return
    quoted_table = schema_editor.quote_name(table_name)
    quoted_column = schema_editor.quote_name(LEGACY_COLUMN)
    schema_editor.execute(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_column}")


class Migration(migrations.Migration):
    dependencies = [("checklists", "0005_normalize_shift_snapshots")]

    operations = [
        migrations.RunPython(
            remove_legacy_membership_column,
            migrations.RunPython.noop,
        )
    ]
