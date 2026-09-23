"""The command policy: what a shell may do unattended, and why almost nothing may.

A wrong "allowed" here means an unattended write. So the tests are written from the attacker's side:
what would look read-only and isn't?
"""

from __future__ import annotations

import pytest

from aiden.tools.command_policy import CommandPolicy, PrefixRule, load_rules


@pytest.fixture
def policy() -> CommandPolicy:
    return CommandPolicy()


# --------------------------------------------------------------------------- read-only


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la src",
        "cat README.md",
        "head -20 aiden/loop.py",
        "tail -5 out.log",
        "wc -l aiden/*.py",
        "grep -rn TODO aiden",
        "rg --files",
        "git status",
        "git diff HEAD",
        "git log --oneline -5",
        "git show HEAD",
        "git ls-files",
        "pytest --collect-only",
        "uv run pytest --collect-only",
    ],
)
def test_read_only_commands_run_unattended(command: str, policy: CommandPolicy):
    assert policy.is_read_only(command), f"{command!r} should not need approval"


# --------------------------------------------------------------------------- unprovable


@pytest.mark.parametrize(
    "command",
    [
        # The whole point: an allowed prefix plus a redirect writes a file.
        "cat a.txt > b.txt",
        "cat a.txt >> b.txt",
        "ls > files.txt",
        "cat < input.txt",
        # Composition makes the effect undecidable.
        "ls && rm -rf build",
        "ls; rm -rf build",
        "ls || echo failed",
        "cat a.txt | tee b.txt",
        "echo `whoami`",
        "echo $(whoami)",
        "uv run pytest --collect-only && rm -rf .",
        # Bash fixtures
        "cat x >y",
        "git status >log",
    ],
)
def test_unprovable_commands_need_approval(command: str, policy: CommandPolicy):
    assert not policy.is_read_only(command), f"{command!r} must not run unattended"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "find . -delete",
        "find . -exec rm {} ;",
        "git push",
        "git commit -m x",
        "curl x",
    ],
)
def test_mutating_commands_need_approval(command: str, policy: CommandPolicy):
    assert not policy.is_read_only(command)


def test_a_denied_flag_overrides_an_allowed_prefix(policy: CommandPolicy):
    """`find` reads; `find . -delete` does not, and no prefix test can tell them apart."""
    assert policy.is_read_only("find . -name '*.py'")
    assert not policy.is_read_only("find . -name '*.py' -delete")


@pytest.mark.parametrize(
    "command",
    ["git branch -D feature", "git remote add origin url", "git show --output=out.txt HEAD"],
)
def test_destructive_flags_on_allowlisted_prefixes_are_caught(command: str, policy: CommandPolicy):
    assert not policy.is_read_only(command), command


def test_ordinary_read_only_flags_are_not_treated_as_dangerous(policy: CommandPolicy):
    """A gate that cries wolf gets bypassed, so `-r` and `-f` must not be denied globally."""
    for command in ("grep -r TODO aiden", "ls -f", "git log -p --stat", "tail -f /dev/null"):
        assert policy.is_read_only(command), command


def test_a_redirect_inside_a_string_is_not_a_redirect(policy: CommandPolicy):
    """Refusing `git log --grep='>'` would be a false positive that trains people to bypass the gate."""
    assert policy.is_read_only("git log --grep='>'")
    assert policy.is_read_only('rg "a > b" aiden')


def test_an_empty_command_is_not_read_only(policy: CommandPolicy):
    assert not policy.is_read_only("")
    assert not policy.is_read_only("   ")


def test_unbalanced_quotes_are_not_provable(policy: CommandPolicy):
    assert not policy.is_read_only("cat 'unclosed")


def test_a_prefix_must_match_a_whole_word(policy: CommandPolicy):
    """`lsof` must not match the `ls` rule."""
    assert not policy.is_read_only("lsof -i")
    assert not policy.is_read_only("catx foo")


# --------------------------------------------------------------------------- reasons


def test_the_reason_names_the_actual_problem(policy: CommandPolicy):
    assert "metacharacter" in policy.reason("cat a > b")
    assert "not read-only" in policy.reason("find . -delete")
    assert "not a known read-only command" in policy.reason("rm -rf build")
    assert "empty" in policy.reason("")


# --------------------------------------------------------------------------- rule loading


def test_default_rules_are_self_consistent():
    """Each rule's match/not_match examples must hold, or the rule is lying about itself."""
    assert CommandPolicy().validate() == []


def test_a_broken_rule_is_reported_not_silently_wrong():
    rule = PrefixRule(("cat",), match=("dog",))
    assert "claims to match" in rule.validate()

    rule = PrefixRule(("cat",), not_match=("cat food",))
    assert "claims not to match" in rule.validate()


def test_user_rules_add_read_only_prefixes():
    policy = CommandPolicy(extra_rules=(PrefixRule(("mytool", "check")),))
    assert policy.is_read_only("mytool check --all")
    assert not policy.is_read_only("mytool write")


def test_a_user_rule_cannot_bypass_the_metacharacter_rule():
    """The allowlist is a floor, not a way to grant approval to something unprovable."""
    policy = CommandPolicy(extra_rules=(PrefixRule(("mytool",)),))
    assert not policy.is_read_only("mytool --x > out.txt")


def test_load_rules_from_config():
    rules = load_rules(
        [{"prefix": ["just", "test"], "match": ["just test"], "not_match": ["just"]}]
    )
    assert rules[0].prefix == ("just", "test")
    assert CommandPolicy(extra_rules=rules).is_read_only("just test")


def test_load_rules_rejects_a_rule_without_a_prefix():
    with pytest.raises(ValueError) as exc:
        load_rules([{"match": ["x"]}])
    assert "non-empty prefix" in str(exc.value)


def test_load_rules_ignores_non_rule_entries():
    assert load_rules("not a list") == ()
    assert load_rules([42, "junk"]) == ()


def test_load_rules_rejects_a_dict_that_looks_like_a_rule_but_is_not():
    """A dict with keys but no prefix is a config mistake, not junk to skip silently."""
    with pytest.raises(ValueError):
        load_rules([{"nope": 1}])
