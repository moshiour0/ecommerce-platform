"""
The Python and Node ownership tables must agree.

They are separate files because the two shared libraries are copied into
different images and share no path at runtime -- python_common lands on
PYTHONPATH, node-common lands in node_modules -- so neither can read the
other's copy, and a single JSON file would have to be duplicated into both
packages anyway.

Duplication without a check is exactly the drift that made price_cents
ambiguous in the first place, so this parses the JavaScript and compares. It
runs in the Python unit tier because that tier already runs on every push and
needs nothing installed; the cost is a small regex, and the alternative is two
tables that quietly disagree about who owns the price.
"""

import re
from pathlib import Path

from conftest import read_model

REPO = Path(__file__).resolve().parents[2]
NODE_MODULE = REPO / "shared" / "libs" / "node-common" / "read_model.js"

# key: 'value' pairs, once comment lines are gone.
ENTRY = re.compile(r"^\s*(\w+):\s*'([^']+)'\s*,?\s*$")


def node_field_owners():
    """PRODUCT_FIELD_OWNERS as declared in the JavaScript module."""
    source = NODE_MODULE.read_text(encoding="utf-8")
    start = source.index("const PRODUCT_FIELD_OWNERS = {")
    body = source[start:]
    body = body[body.index("{") + 1: body.index("};")]

    owners = {}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = ENTRY.match(line)
        if match:
            owners[match.group(1)] = match.group(2)
    return owners


def test_the_javascript_table_is_parseable():
    # If this fails the parser has drifted from the file, and every other
    # assertion here would pass vacuously.
    owners = node_field_owners()
    assert owners, "no field owners parsed out of read_model.js"
    assert len(owners) >= 5


def test_both_tables_cover_the_same_fields():
    assert set(node_field_owners()) == set(read_model.PRODUCT_FIELD_OWNERS)


def test_every_field_has_the_same_owner_on_both_sides():
    node = node_field_owners()
    for field, owner in read_model.PRODUCT_FIELD_OWNERS.items():
        assert node[field] == owner, (
            f"{field} is owned by {owner} in Python and {node[field]} in "
            f"JavaScript; one of them will overwrite the other's data")


def test_the_owner_names_match():
    # A rename on one side only would make every validate call on that side
    # reject writes the other side accepts.
    assert set(node_field_owners().values()) == set(
        read_model.PRODUCT_FIELD_OWNERS.values())


def test_the_shared_owner_sentinel_matches():
    node = node_field_owners()
    shared_python = {f for f, o in read_model.PRODUCT_FIELD_OWNERS.items()
                     if o == read_model.ANY_OWNER}
    shared_node = {f for f, o in node.items() if o == "*"}
    assert shared_python == shared_node
