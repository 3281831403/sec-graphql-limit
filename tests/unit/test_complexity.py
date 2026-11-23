"""gql.complexity 的十条契约测试，每条对应 README 中的一个缺陷。"""

import pytest

from gql.complexity import (
    check_limits,
    cost_of,
    depth_of,
    expand_fragments,
    field_count,
    parse_query,
    query_batch,
    validate_policy,
)

POLICY = {
    "max_cost": 1000.0,
    "max_depth": 10,
    "max_fields": 100,
    "max_fragments": 10,
}


def test_01_nested_fields_are_counted():
    doc = parse_query("{ viewer { id name friends { id name } } }")
    assert field_count(doc, alias_aware=True) == 6


def test_02_fragment_spread_is_expanded_before_counting():
    doc = parse_query(
        """
        query { viewer { ...UserFields } }
        fragment UserFields on User { id name friends { id } }
        """
    )
    expanded = expand_fragments(doc, doc["fragments"])
    assert field_count(expanded, alias_aware=True) == 5


def test_03_fragment_cycle_is_rejected():
    doc = parse_query(
        """
        query { ...A }
        fragment A on User { id ...B }
        fragment B on User { name ...A }
        """
    )
    with pytest.raises(ValueError):
        expand_fragments(doc, doc["fragments"])


def test_04_alias_does_not_add_depth():
    doc = parse_query("{ a: user { b: profile { c: name } } }")
    assert depth_of(doc) == 3


def test_05_alias_amplification_is_counted():
    doc = parse_query("{ a: user { id } b: user { id } c: user { id } }")
    assert field_count(doc, alias_aware=True) == 6


def test_06_list_field_cost_uses_multiplier():
    doc = parse_query("{ users { id } }")
    weights = {
        "fields": {"users": 1.0, "id": 1.0},
        "list_fields": ["users"],
        "list_multiplier": 10.0,
    }
    assert cost_of(doc, weights) == 20.0


def test_07_any_single_limit_exceeded_is_rejected():
    policy = dict(POLICY, max_cost=100.0, max_depth=5, max_fields=10)
    verdict = check_limits(120.0, 3, 4, policy)
    assert verdict["allowed"] is False
    assert verdict["violations"] == ["cost"]


def test_08_limits_are_inclusive_at_boundary():
    policy = dict(POLICY, max_cost=100.0, max_depth=3, max_fields=5)
    verdict = check_limits(100.0, 3, 5, policy)
    assert verdict["allowed"] is True
    assert verdict["violations"] == []


def test_09_policy_without_cost_limit_is_reported():
    policy = {"max_depth": 5, "max_fields": 10, "max_fragments": 10}
    assert validate_policy(policy) == ["missing_cost_limit"]


def test_10_batch_queries_do_not_share_parsed_structure():
    first = "query { alpha { ...Shared } } fragment Shared on T { one }"
    second = "query { beta { ...Shared } } fragment Shared on T { two three }"
    batch = query_batch([first, second], POLICY)
    assert [item["fields"] for item in batch["results"]] == [2, 3]
    assert batch["allowed"] is True
    assert batch["results"][0] == query_batch([first], POLICY)["results"][0]
    assert batch["results"][1] == query_batch([second], POLICY)["results"][0]