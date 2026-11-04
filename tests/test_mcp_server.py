"""Tests for the MCP adapter.

WHAT THESE ASSERT, AND WHY IT IS MOSTLY STRUCTURE
-------------------------------------------------
The adapter implements no verb of its own -- every tool calls the same function a
Python consumer calls -- so testing that a trash works would re-test `test_writes.py`.
What is genuinely new and genuinely fragile is the SCHEMA: 17 declarations that have to
keep agreeing with 17 signatures, in a file where a typo produces a tool that fails only
when an agent reaches for it. So most of these are properties over `TOOLS`.

Three of them encode requirements that would be silent if broken:

  * every declared property is a real parameter of the verb (a typo'd `item_keys` would
    otherwise reach `TypeError` at call time, in front of an agent)
  * no tool declares `store`, `linker`, `cookjohn` or `journal_dir` -- the first three
    would let a caller aim the write path at an arbitrary URL, and `test_crud.py`
    already asserts the same property of the Python signatures
  * no schema ships `force` defaulted to True, and every verb that HAS a force gate
    exposes it -- defaulting it would silently defeat the gates, and omitting it would
    make those verbs permanently unusable through MCP

WHERE THESE MOCK
----------------
At the transports and the database, which is the same boundary `conftest.py` uses. The
tool call path deliberately has no `store`/`linker`/`cookjohn` parameter, so a test
cannot inject through it -- instead the fakes are substituted for the CLASSES each
module constructs, leaving the dispatch, the argument filtering, the gates, the journal
and the read-back verification all running for real. Nothing here needs Zotero running;
the autouse fixture in `conftest.py` makes a real HTTP call raise.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from zotero_core.application.services.collections import (
    add_items_to_collection,
    create_collection,
    delete_collection,
    remove_items_from_collection,
    update_collection,
)
from zotero_core.application.services.verbs import (
    add_tags,
    create_item,
    import_attachment,
    link_attachment,
    remove_tags,
    replace_creators,
    restore_items,
    set_tags,
    trash_items,
    update_metadata,
    write_note,
)
from zotero_core.domain.errors import ALL_REASONS, WriteBlocked
from zotero_core.interfaces import write_mcp as adapter

# The package's whole public write surface, as `__init__.__all__` groups it. A verb
# added to the package and not exposed here fails `test_every_public_write_verb_...`.
PUBLIC_WRITE_VERBS = [
    create_item,
    link_attachment,
    import_attachment,
    write_note,
    create_collection,
    update_metadata,
    add_tags,
    remove_tags,
    set_tags,
    replace_creators,
    update_collection,
    add_items_to_collection,
    remove_items_from_collection,
    trash_items,
    restore_items,
    delete_collection,
]

# Injection seams and the audit-trail path. None may appear in a tool schema.
FORBIDDEN_PROPERTIES = ("store", "linker", "cookjohn", "journal_dir")


@pytest.fixture()
def wired(zotero):
    """The temp library the MCP tests write against.

    ⚠ THIS FIXTURE USED TO MONKEYPATCH FIVE MODULE GLOBALS, and its docstring explained
    why: "the verbs resolve their transports through the module-global name, so patching
    the name is enough -- and it is the only way in, because the MCP surface has no
    injection parameter by design." Both halves are now false. The verbs take a
    `WriteSession`, and `call_writes` takes one too, so the tests below pass
    `session=session` and nothing is rewritten from under the code.

    Two of those patches also used `raising=False`, which means a renamed module would
    have silently stopped being patched -- a test could believe it held a fake and be
    talking to a real client. Injection cannot fail that way: a missing argument is a
    TypeError, not a no-op.
    """
    return zotero


# --------------------------------------------------------------------------
# the schema and the signatures must agree — 17 pairs, checked rather than trusted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spec", adapter.TOOLS, ids=lambda s: s.name)
def test_every_declared_property_is_a_real_parameter_of_the_verb(spec):
    """A typo'd property name produces a tool that raises TypeError the first time an
    agent uses it, with nothing at import or in a lint run to catch it."""
    parameters = inspect.signature(spec.verb).parameters
    for prop in spec.properties:
        assert prop in parameters, (
            f"{spec.name} declares {prop!r}, not a parameter of {spec.verb.__name__}"
        )


@pytest.mark.parametrize("spec", adapter.TOOLS, ids=lambda s: s.name)
def test_every_required_property_is_declared(spec):
    """`required` naming something absent from `properties` is a schema a client rejects."""
    assert set(spec.required) <= set(spec.properties)


@pytest.mark.parametrize("spec", adapter.TOOLS, ids=lambda s: s.name)
def test_no_tool_exposes_a_transport_seam_or_the_journal_path(spec):
    """`store`/`linker`/`cookjohn` are test seams; exposing them would let a caller aim
    the write path at an arbitrary URL, and `test_crud.py` asserts the same property of
    the Python signatures. `journal_dir` is withheld for a different reason: the journal
    is only an audit trail of real writes if every write lands in one place."""
    for name in FORBIDDEN_PROPERTIES:
        assert name not in spec.properties, f"{spec.name} exposes {name}"


def test_tool_names_are_unique_and_namespaced():
    names = [spec.name for spec in adapter.TOOLS]
    assert len(names) == len(set(names))
    # cookjohn's own MCP registers `write_item`, `write_note`, `write_metadata`,
    # `create_collection` and both collection-membership verbs under those bare names.
    # The prefix keeps an agent's tool list unambiguous about which surface is gated.
    assert all(name.startswith("zotero_") for name in names)


def test_every_public_write_verb_is_reachable_through_mcp():
    """The gap this adapter closes was a verb with no MCP tool — trash, which lives on
    the linker and which an agent had to drive through `uv run python` one-liners. A
    partial mirror would recreate exactly that."""
    exposed = {spec.verb for spec in adapter.TOOLS}
    missing = [verb.__name__ for verb in PUBLIC_WRITE_VERBS if verb not in exposed]
    assert not missing, f"public write verbs with no MCP tool: {missing}"


def test_the_schemas_are_wellformed_json_schema_objects():
    for spec in adapter.TOOLS:
        schema = spec.as_tool_schema()
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        for prop, body in schema["properties"].items():
            assert "type" in body, f"{spec.name}.{prop} declares no type"
        json.dumps(schema)  # a client receives this over the wire


# --------------------------------------------------------------------------
# force: exposed everywhere it exists, never defaulted on
# --------------------------------------------------------------------------


def test_no_schema_defaults_force_to_true():
    """The one requirement that would silently defeat the gates rather than break them.
    `set_tags` and `replace_creators` refuse without it, `delete_collection`'s cascade
    refuses without it, and a schema default of True would make all three pass by
    themselves."""
    for spec in adapter.TOOLS:
        if "force" in spec.properties:
            assert spec.properties["force"]["default"] is False, spec.name
            assert "force" not in spec.required, f"{spec.name} makes force mandatory"


def test_every_verb_with_a_force_gate_exposes_it():
    """The inverse failure: omitting `force` from the schema would make `set_tags` and
    `replace_creators` permanently refuse through MCP, with no way to complete the
    deliberate second ask."""
    for spec in adapter.TOOLS:
        if "force" in inspect.signature(spec.verb).parameters:
            assert "force" in spec.properties, f"{spec.name} hides its force gate"


# --------------------------------------------------------------------------
# WriteBlocked is DATA — the reason this exists rather than a python one-liner
# --------------------------------------------------------------------------


def test_a_blocked_write_renders_as_structured_data_with_a_stable_code(wired, session):
    """A precondition failure carries `code` (branchable), `reason` (prose for a human)
    and `detail` (what is there now and would have been lost). A traceback discards all
    three, which is what a `uv run python` one-liner gave the caller."""
    wired.add("ABCD2345", "A Paper", tags=["curated", "read"])
    with pytest.raises(WriteBlocked) as caught:
        adapter.call_writes(
            "zotero_set_tags", {"item_key": "ABCD2345", "tags": ["new"]}, session=session
        )

    payload = caught.value.as_dict()
    assert payload["ok"] is False
    assert payload["code"] in ALL_REASONS
    assert payload["code"] == "refusing_to_replace"
    # The detail is the part that makes the refusal actionable rather than just a no.
    assert sorted(payload["detail"]["current_tags"]) == ["curated", "read"]
    assert payload["detail"]["would_become"] == ["new"]
    # And it survives serialisation, which is what the transport actually sends.
    assert json.loads(adapter._render(payload))["code"] == "refusing_to_replace"


def test_every_reason_code_the_adapter_can_emit_is_in_the_published_set(wired, session):
    """`code` is only a contract if it comes from the documented set. `ALL_REASONS` is
    what a consumer branches on."""
    wired.add("ABCD2345", "A Paper")
    for tool, arguments in (
        ("zotero_create_item", {"item_type": "book", "fields": {}}),
        ("zotero_trash_items", {"item_keys": ["NOSUCH12"]}),
        ("zotero_update_metadata", {"item_key": "ABCD2345", "fields": {}}),
        ("zotero_link_attachment", {"parent_item_key": "ABCD2345", "path": "rel/x.pdf"}),
    ):
        with pytest.raises(WriteBlocked) as caught:
            adapter.call_writes(tool, arguments, session=session)
        assert caught.value.code in ALL_REASONS, tool


# --------------------------------------------------------------------------
# the gates are not weakened by going through MCP
# --------------------------------------------------------------------------


def test_replacing_creators_through_mcp_still_needs_the_second_ask(wired, session):
    wired.add("ABCD2345", "A Paper", creators=[{"firstName": "A", "lastName": "Author"}])
    with pytest.raises(WriteBlocked) as caught:
        adapter.call_writes(
            "zotero_replace_creators",
            {"item_key": "ABCD2345", "creators": [{"firstName": "B", "lastName": "Other"}]},
            session=session,
        )
    assert caught.value.code == "refusing_to_replace"
    assert caught.value.detail["current_creators"][0]["lastName"] == "Author"


def test_force_passed_through_mcp_completes_the_replacement(wired, session):
    """`force` has to actually reach the verb — a schema that declared it while the
    dispatch dropped it would leave the gated verbs unusable."""
    wired.add("ABCD2345", "A Paper", creators=[{"firstName": "A", "lastName": "Author"}])
    out = adapter.call_writes(
        "zotero_replace_creators",
        {
            "item_key": "ABCD2345",
            "creators": [{"creatorType": "author", "firstName": "B", "lastName": "Other"}],
            "force": True,
        },
        session=session,
    )
    assert out["ok"] is True
    assert out["creators_before"][0]["lastName"] == "Author"
    assert out["undo_call"].startswith("replace_creators('ABCD2345'")


def test_a_no_op_update_is_still_refused_through_mcp(wired, session):
    wired.add("ABCD2345", "A Paper", fields={"publisher": "ACM"})
    with pytest.raises(WriteBlocked) as caught:
        adapter.call_writes(
            "zotero_update_metadata",
            {"item_key": "ABCD2345", "fields": {"publisher": "ACM"}},
            session=session,
        )
    assert caught.value.code == "nothing_to_do"


def test_a_batch_with_one_unresolvable_key_is_refused_whole(wired, session):
    """The gate that motivated the package: the plugin would trash the resolvable ones
    and return success."""
    wired.add("ABCD2345", "A Paper")
    with pytest.raises(WriteBlocked) as caught:
        adapter.call_writes(
            "zotero_trash_items", {"item_keys": ["ABCD2345", "NOSUCH12"]}, session=session
        )
    assert caught.value.code == "unknown_item_keys"


def test_trash_and_restore_round_trip_through_mcp(wired, linker, session):
    """The verb that had no MCP tool at all, end to end over the adapter."""
    wired.add("ABCD2345", "A Paper")
    trashed = adapter.call_writes(
        "zotero_trash_items", {"item_keys": ["ABCD2345"]}, session=session
    )
    assert trashed["ok"] is True
    assert trashed["transport"] == "linker"
    assert wired.is_trashed("ABCD2345") is True

    restored = adapter.call_writes(
        "zotero_restore_items", {"item_keys": ["ABCD2345"]}, session=session
    )
    assert restored["ok"] is True
    assert wired.is_trashed("ABCD2345") is False
    assert [path for path, _ in linker.posts] == ["trash-items", "restore-items"]


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def test_an_unknown_tool_is_named_in_the_error(session):
    with pytest.raises(ValueError, match="Unknown tool: zotero_nope"):
        adapter.call_writes("zotero_nope", {}, session=session)


def test_an_undeclared_argument_is_refused_rather_than_dropped(wired, session):
    """Dropping it would give a caller who passed `journal_dir` a silent no-op on the
    argument they cared about, and a caller who misspelled `force` the refusal they were
    trying to pass."""
    wired.add("ABCD2345", "A Paper")
    with pytest.raises(ValueError, match="does not accept"):
        adapter.call_writes(
            "zotero_set_tags",
            {"item_key": "ABCD2345", "tags": ["x"], "journal_dir": "/tmp/elsewhere"},
            session=session,
        )


def test_an_absent_required_argument_is_named(session):
    with pytest.raises(ValueError, match=r"requires \['item_keys'\]"):
        adapter.call_writes("zotero_trash_items", {}, session=session)


def test_an_explicit_null_on_an_optional_argument_falls_back_to_the_default(wired, session):
    """Some clients send every declared property. `action=None` reaching `write_note`
    would defeat its default of "create" and refuse with `missing_required_field`."""
    wired.add("ABCD2345", "A Paper")
    out = adapter.call_writes(
        "zotero_write_note",
        {"content": "<p>hi</p>", "parent_item_key": "ABCD2345", "action": None, "note_key": None},
        session=session,
    )
    assert out["op"] == "write_note:create"


def test_a_null_on_a_required_argument_reaches_the_verbs_coded_gate(session):
    """Passed through on purpose: `WriteBlocked` with a code beats a ValueError from the
    adapter, because a code is what a caller can branch on."""
    with pytest.raises(WriteBlocked) as caught:
        adapter.call_writes("zotero_trash_items", {"item_keys": None}, session=session)
    assert caught.value.code == "no_item_keys"


# --------------------------------------------------------------------------
# preflight — read-only, and reports the two plugins separately
# --------------------------------------------------------------------------


def test_preflight_reports_each_transport_separately(session):
    """ "Zotero is up and one plugin is missing" is a different job to fix than "Zotero
    is closed", and it decides which verbs are available. `require_zotero` raises on the
    first failure, which is right for a write and wrong for a preflight."""
    out = adapter.call_writes("zotero_write_preflight", {}, session=session)
    assert out["ok"] is True
    assert out["transports"]["linker"]["available"] is True
    assert out["transports"]["linker"]["info"]["plugin"] == "zotero-linker"
    assert out["transports"]["cookjohn"]["available"] is True


def test_preflight_resolves_keys_without_writing_anything(wired, session, linker, cookjohn):
    wired.add("ABCD2345", "A Paper", item_type="book")
    wired.add("ATT12345", "scan.pdf", "attachment", parent="ABCD2345")
    out = adapter.call_writes(
        "zotero_write_preflight", {"item_keys": ["ABCD2345"]}, session=session
    )

    resolved = out["items"]["resolved"][0]
    assert resolved == {
        "key": "ABCD2345",
        "title": "A Paper",
        "item_type": "book",
        "trashed": False,
        "child_keys": ["ATT12345"],
    }
    # Read-only is the whole point: nothing was POSTed and no tool was called.
    assert linker.posts == []
    assert cookjohn.calls == []


def test_preflight_reports_an_unresolvable_key_as_data(session):
    out = adapter.call_writes(
        "zotero_write_preflight", {"item_keys": ["NOSUCH12"]}, session=session
    )
    assert out["ok"] is False
    assert out["items"]["code"] == "unknown_item_keys"


# --------------------------------------------------------------------------
# `mcp` must not be in the write path's import graph
# --------------------------------------------------------------------------


def test_mcp_is_imported_inside_a_function_never_at_module_level():
    """`dependencies = []` is the promise this protects. A module-level `import mcp` here
    would make every consumer of `zotero_core.application` need the extra — `omni-rag`
    imports this package inside its ARC entrypoints, where an async runtime pulled in by a
    tag verb is a real cost.

    ⚠ This docstring previously justified the rule by claiming `cookjohn.py` is vendored into
    `calibre-zotero-jump`. Nothing is vendored there; the rule stands on the line above.

    Checked against the source rather than `sys.modules`, which another test may have
    populated. `.importlinter` enforces the same rule from the other direction."""
    source = Path(adapter.__file__).read_text()
    tree = ast.parse(source)
    module_level = []
    for node in tree.body:  # top level only — a function body is not in tree.body
        if isinstance(node, ast.Import):
            module_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_level.append(node.module or "")
    assert not [name for name in module_level if name.split(".")[0] == "mcp"], (
        f"mcp imported at module level: {module_level}"
    )


def test_the_adapter_imports_without_the_mcp_extra_installed():
    """Follows from the test above, asserted directly: the module has to be importable
    for `TOOLS` to be inspectable and for the entry point to reach its own error
    message ("uv sync --extra mcp") instead of an ImportError traceback."""
    assert adapter.SERVER_NAME == "zotero-writes"
    assert callable(adapter.run)
