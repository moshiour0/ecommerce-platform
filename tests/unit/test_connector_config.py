"""
Unit tests for Debezium connector provisioning.

These exist because the connector configuration has been wrong twice, and both
times the symptom appeared somewhere else entirely.

Eight hand-written JSON files under workers/cdc-outbox defined connectors with
no slot.name, so every one of them fell back to Postgres's default "debezium"
replication slot and fought over it. They also covered only eight of the
fifteen databases. They are deleted now, and fix_connectors.py is the single
provisioning path -- these tests are what makes that path checkable, since a
config posted to a running Debezium fails in ways that show up as missing
events rather than as an error at the point of the mistake.
"""

from conftest import connector_config as fc

DATABASES = fc.DATABASES
build_config = fc.build_config


def all_configs():
    return {db: build_config(db, svc) for db, svc in DATABASES.items()}


# ---------------------------------------------------------------------------
# replication slots
# ---------------------------------------------------------------------------

def test_every_connector_has_a_slot_name():
    # Without it Postgres uses the default slot, and connectors that share a
    # slot fight over it: some databases stop producing events entirely.
    for db, cfg in all_configs().items():
        assert cfg.get("slot.name"), f"{db} has no slot.name"


def test_slot_names_are_unique():
    # The actual failure mode. Two connectors on one slot is not a warning.
    slots = [cfg["slot.name"] for cfg in all_configs().values()]
    assert len(slots) == len(set(slots)), \
        f"duplicate replication slots: {sorted(s for s in slots if slots.count(s) > 1)}"


def test_connector_names_are_unique():
    names = [f"{svc}-outbox-connector" for svc in DATABASES.values()]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# the event type header
# ---------------------------------------------------------------------------

def test_every_connector_carries_the_type_header():
    # Without this the consumer infers an event's type from the shape of its
    # fields, and that guess indexed every failed inventory reservation as a
    # null-filled product.
    for db, cfg in all_configs().items():
        placement = cfg["transforms.outbox.table.fields.additional.placement"]
        assert "type:header:eventType" in placement, \
            f"{db} does not carry the event type"


def test_the_type_is_a_header_not_an_envelope_field():
    # The envelope is part of the Avro value schema and the registry runs
    # FULL_TRANSITIVE, which rejects the change and fails the connector task.
    for cfg in all_configs().values():
        placement = cfg["transforms.outbox.table.fields.additional.placement"]
        assert "type:envelope" not in placement


def test_the_created_at_header_is_still_there():
    for cfg in all_configs().values():
        assert "created_at:header:timestamp" in \
            cfg["transforms.outbox.table.fields.additional.placement"]


# ---------------------------------------------------------------------------
# databases and routing
# ---------------------------------------------------------------------------

def test_each_config_points_at_its_own_database():
    for db, cfg in all_configs().items():
        assert cfg["database.dbname"] == db


def test_database_names_are_not_derived_by_stripping_a_suffix():
    # promo_db, payment_ledger_db and media_meta_db do not follow the pattern.
    # Deriving the slug from the database name is what pointed three connectors
    # at databases that have never existed.
    assert DATABASES["promo_db"] == "promotion"
    assert DATABASES["payment_ledger_db"] == "payment"
    assert DATABASES["media_meta_db"] == "media"


def test_only_the_outbox_table_is_captured():
    # Capturing business tables would put private data on Kafka and defeat the
    # point of the outbox pattern (Rule 3).
    for cfg in all_configs().values():
        assert cfg["table.include.list"] == "public.outbox_messages"


def test_topics_are_routed_by_aggregate_type():
    for cfg in all_configs().values():
        assert cfg["transforms.outbox.route.by.field"] == "aggregate_type"
        assert cfg["transforms.outbox.route.topic.replacement"] == \
            "${routedByValue}.events"


def test_topic_prefixes_are_unique():
    prefixes = [cfg["topic.prefix"] for cfg in all_configs().values()]
    assert len(prefixes) == len(set(prefixes))


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def test_credentials_are_overridable():
    # The cluster's Postgres password is generated into a Secret, so a literal
    # here failed every connector with "password authentication failed".
    cfg = build_config("catalog_db", "catalog", pg_user="u", pg_password="p",
                       pg_host="h")
    assert cfg["database.user"] == "u"
    assert cfg["database.password"] == "p"
    assert cfg["database.hostname"] == "h"


def test_every_database_that_exists_gets_a_connector():
    """The connector map must cover every database init-dbs.sh creates.

    The original form of this test asserted a hardcoded 15, which caught the
    gap it was written for -- the deleted JSON files covered eight of fifteen,
    and the seven missing services simply never produced events -- but it made
    adding a sixteenth database fail for the wrong reason. Reading init-dbs.sh
    instead means the assertion is about the actual defect: a service database
    whose outbox nothing is watching.
    """
    import re
    from pathlib import Path

    init_sh = (Path(__file__).resolve().parent.parent.parent
               / "init-dbs.sh").read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE DATABASE (\w+);", init_sh))

    assert created, "no CREATE DATABASE statements found in init-dbs.sh"

    missing = created - set(DATABASES) - DATABASES_WITHOUT_AN_OUTBOX
    assert not missing, (
        f"database(s) with no Debezium connector: {sorted(missing)}. Their "
        f"outbox rows will never reach Kafka, and nothing will report it.")


# Databases that deliberately publish nothing, and why.
#
# An exemption list is a place bugs hide, so it is short and each entry has to
# justify itself. The test above is about a database whose outbox nothing
# watches; a database with no outbox at all is a different thing, and giving it
# a connector would provision a replication slot for a table that does not
# exist.
DATABASES_WITHOUT_AN_OUTBOX = frozenset({
    # behaviour_db records what buyers looked at. Nothing downstream consumes
    # it: affinity is read directly by search-service, and publishing a stream
    # of "this person viewed that product" into Kafka -- where it is retained
    # and read by consumers this service does not know about -- would copy the
    # most personal data on the platform into every one of their stores.
    "behaviour_db",
})


def test_every_exempt_database_really_has_no_outbox():
    """The exemption is checkable rather than trusted.

    A database that grows an outbox later and stays on this list would publish
    nothing and report nothing, which is exactly the failure the test above
    exists to catch.
    """
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[2] / "migrations"
    for database in DATABASES_WITHOUT_AN_OUTBOX:
        for path in migrations.glob(f"*{database.replace('_db', '_db')}*.sql"):
            sql = path.read_text(encoding="utf-8")
            statements = re.findall(
                r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
            assert "outbox_messages" not in statements, (
                f"{path.name} creates an outbox in {database}, which is on "
                f"the no-connector exemption list. Either remove the "
                f"exemption or remove the outbox.")


def test_no_connector_points_at_a_database_that_does_not_exist():
    """The other direction: a slot provisioned for nothing.

    Split back out of the coverage test above, where it had been sharing that
    test's local `created` -- an edit inserted the exemption list between the
    two halves and orphaned this one.
    """
    import re
    from pathlib import Path

    init_sh = (Path(__file__).resolve().parents[2]
               / "init-dbs.sh").read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE DATABASE (\w+);", init_sh))

    unknown = set(DATABASES) - created
    assert not unknown, (
        f"connector(s) for database(s) init-dbs.sh does not create: "
        f"{sorted(unknown)}")

    assert len(all_configs()) == len(DATABASES)
