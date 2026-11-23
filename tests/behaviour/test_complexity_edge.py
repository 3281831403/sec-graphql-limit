"""gql.complexity 的五条边界测试，覆盖端点与批量独立性。"""

from gql.complexity import (
    check_limits,
    cost_of,
    depth_of,
    expand_fragments,
    field_count,
    parse_query,
    query_batch,
)

BASE_POLICY = {
    "max_cost": 100.0,
    "max_depth": 5,
    "max_fields": 20,
    "max_fragments": 10,
    "cost_weights": {"fields": {}, "list_fields": [], "list_multiplier": 1.0},
}


def test_simple_query_passes():
    doc = parse_query("{ viewer { id name } }")
    expanded = expand_fragments(doc, doc["fragments"])
    depth = depth_of(expanded)
    fields = field_count(expanded, alias_aware=True)
    cost = cost_of(expanded, BASE_POLICY["cost_weights"])
    verdict = check_limits(cost, depth, fields, BASE_POLICY)
    assert (depth, fields, cost) == (2, 3, 3.0)
    assert verdict["allowed"] is True
    assert verdict["violations"] == []


def test_nested_too_deep_is_rejected():
    doc = parse_query("{ l1 { l2 { l3 { l4 { l5 { l6 } } } } } }")
    expanded = expand_fragments(doc, doc["fragments"])
    depth = depth_of(expanded)
    fields = field_count(expanded, alias_aware=True)
    cost = cost_of(expanded, BASE_POLICY["cost_weights"])
    verdict = check_limits(cost, depth, fields, BASE_POLICY)
    assert depth == 6
    assert verdict["allowed"] is False
    assert verdict["violations"] == ["depth"]


def test_alias_amplification_counts_toward_field_limit():
    doc = parse_query("{ a: item { id } b: item { id } c: item { id } }")
    expanded = expand_fragments(doc, doc["fragments"])
    depth = depth_of(expanded)
    fields = field_count(expanded, alias_aware=True)
    cost = cost_of(expanded, BASE_POLICY["cost_weights"])
    policy = dict(BASE_POLICY, max_fields=5)
    verdict = check_limits(cost, depth, fields, policy)
    assert fields == 6
    assert verdict["allowed"] is False
    assert verdict["violations"] == ["fields"]


def test_fragment_expansion_still_respects_limits():
    doc = parse_query(
        """
        query { viewer { ...Wide } }
        fragment Wide on User { f1 f2 f3 f4 f5 f6 }
        """
    )
    expanded = expand_fragments(doc, doc["fragments"])
    depth = depth_of(expanded)
    fields = field_count(expanded, alias_aware=True)
    cost = cost_of(expanded, BASE_POLICY["cost_weights"])
    policy = dict(BASE_POLICY, max_fields=5)
    verdict = check_limits(cost, depth, fields, policy)
    assert fields == 7
    assert verdict["allowed"] is False
    assert verdict["violations"] == ["fields"]


def test_batch_results_equal_individual_evaluations():
    first = "query { alpha { ...Shared } } fragment Shared on T { one }"
    second = "query { beta { ...Shared } } fragment Shared on T { two three }"
    batch = query_batch([first, second], BASE_POLICY)
    individual_first = query_batch([first], BASE_POLICY)
    individual_second = query_batch([second], BASE_POLICY)
    assert batch["results"][0] == individual_first["results"][0]
    assert batch["results"][1] == individual_second["results"][0]
    assert [item["fields"] for item in batch["results"]] == [2, 3]