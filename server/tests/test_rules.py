from app.rules import RulesEngine, local_display, max_severity, normalise_number
from app.settings import Settings


def test_normalise_forms():
    assert normalise_number("+919812345678") == "+919812345678"
    assert normalise_number("919812345678") == "+919812345678"
    assert normalise_number("09812345678") == "+919812345678"
    assert normalise_number("9812345678") == "+919812345678"
    assert normalise_number("00919812345678") == "+919812345678"
    assert normalise_number("+91 98123-45678") == "+919812345678"


def test_withheld_numbers_are_empty():
    for raw in (None, "", "  ", "Unknown", "withheld", "PRIVATE"):
        assert normalise_number(raw) == ""


def test_short_codes_kept():
    assert normalise_number("121") == "121"


def test_local_display():
    assert local_display("+919812345678") == "9812345678"
    assert local_display("121") == "121"


def test_max_severity():
    assert max_severity("low", "normal") == "normal"
    assert max_severity("urgent", "normal") == "urgent"
    assert max_severity("high", None) == "high"


def test_lookup_groups(db, config):
    rules = RulesEngine(db, Settings(config, db), 180)
    family = db.group_by_name("family")["id"]
    blocked = db.group_by_name("blocked")["id"]
    db.add_contact("Amma", "+919811111111", family, language_hint="kn")
    db.add_contact("Spammer", "+919822222222", blocked)
    db.add_contact("Ravi", "+919833333333", None)

    r = rules.lookup("+919811111111")
    assert (r.action, r.group, r.language_hint, r.severity_floor) == ("answer", "family", "kn", "normal")
    assert "Ani will call back" in r.extra_prompt

    assert rules.lookup("+919822222222").action == "reject"
    assert rules.lookup("+919833333333").group == "known"     # contact without a group
    unknown = rules.lookup("+919844444444")
    assert unknown.group == "unknown" and unknown.contact_id is None
    assert rules.lookup("").group == "unknown"                # withheld


def test_rule_edit_changes_behaviour(db, config):
    rules = RulesEngine(db, Settings(config, db), 180)
    unknown_rule = db.group_by_name("unknown")["rule_id"]
    db.update_rule(unknown_rule, action="ignore", max_seconds=60)
    r = rules.lookup("+919844444444")
    assert r.action == "ignore" and r.max_seconds == 60
