from django.db import migrations

SQL = '''
CREATE OR REPLACE FUNCTION protect_anumati_audit() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Audit events are append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER core_audit_immutable BEFORE UPDATE OR DELETE ON core_auditevent
FOR EACH ROW EXECUTE FUNCTION protect_anumati_audit();
'''

class Migration(migrations.Migration):
    dependencies = [('core', '0001_initial')]
    operations = [migrations.RunSQL(SQL, 'DROP TRIGGER IF EXISTS core_audit_immutable ON core_auditevent; DROP FUNCTION IF EXISTS protect_anumati_audit();')]
