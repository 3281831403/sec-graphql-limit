"""GraphQL 查询限流核心契约。

本模块只依赖标准库。

AST 约定（parse_query 的返回值）::

    document = {
        "kind": "document",
        "operation": "query",
        "name": str | None,
        "variables": {变量名: 类型名},
        "selection": [field, ...],
        "fragments": {
            片段名: {
                "kind": "fragment",
                "name": 片段名,
                "type_condition": 类型名,
                "selection": [field, ...],
            },
        },
    }

    field = {
        "kind": "field",
        "name": 字段名,
        "alias": 别名或 None,
        "arguments": {参数名: 原始文本},
        "selection": [field, ...],
    }

    spread = {"kind": "spread", "name": 片段名}

所有数值比较的容差 0：不设相对误差，也不设绝对误差。
"""

from typing import Any

__all__ = [
    "parse_query",
    "expand_fragments",
    "depth_of",
    "field_count",
    "cost_of",
    "check_limits",
    "validate_policy",
    "query_batch",
]


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

_PUNCT = set("{}():$[]!,")


def _tokenize(text: str) -> list[tuple[str, str, int, int]]:
    """把查询文本切成 (kind, value, start, end) 词元序列，末尾附 EOF。"""
    tokens: list[tuple[str, str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace() or c == ",":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("SPREAD", "...", i, i + 3))
            i += 3
            continue
        if c in _PUNCT:
            tokens.append(("PUNCT", c, i, i + 1))
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            if j >= n:
                raise ValueError("unterminated string literal")
            tokens.append(("STRING", text[i : j + 1], i, j + 1))
            i = j + 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("NAME", text[i:j], i, j))
            i = j
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            tokens.append(("NUMBER", text[i:j], i, j))
            i = j
            continue
        raise ValueError(f"unexpected character {c!r} at offset {i}")
    tokens.append(("EOF", "", n, n))
    return tokens


def _is(tok: tuple[str, str, int, int], kind: str, value: str | None = None) -> bool:
    return tok[0] == kind and (value is None or tok[1] == value)


class _Parser:
    def __init__(self, text: str, tokens: list[tuple[str, str, int, int]]):
        self.text = text
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str, int, int]:
        return self.tokens[self.pos]

    def advance(self) -> tuple[str, str, int, int]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str, int, int]:
        tok = self.peek()
        if not _is(tok, kind, value):
            want = value if value is not None else kind
            raise ValueError(f"expected {want!r}, got {tok[1]!r}")
        return self.advance()

    # -- 文档层级 ----------------------------------------------------------

    def parse_selection_set(self) -> list[dict[str, Any]]:
        self.expect("PUNCT", "{")
        selection: list[dict[str, Any]] = []
        while not _is(self.peek(), "PUNCT", "}"):
            if self.peek()[0] == "EOF":
                raise ValueError("unterminated selection set")
            selection.append(self.parse_selection())
        self.expect("PUNCT", "}")
        if not selection:
            raise ValueError("empty selection set")
        return selection

    def parse_selection(self) -> dict[str, Any]:
        tok = self.peek()
        if tok[0] == "SPREAD":
            self.advance()
            name = self.expect("NAME")[1]
            return {"kind": "spread", "name": name}
        if tok[0] != "NAME":
            raise ValueError(f"unexpected token {tok[1]!r} in selection set")
        first = self.advance()[1]
        alias: str | None = None
        name = first
        if _is(self.peek(), "PUNCT", ":"):
            self.advance()
            alias = first
            name = self.expect("NAME")[1]
        arguments: dict[str, str] = {}
        if _is(self.peek(), "PUNCT", "("):
            arguments = self.parse_arguments()
        sub: list[dict[str, Any]] = []
        if _is(self.peek(), "PUNCT", "{"):
            sub = self.parse_selection_set()
        return {
            "kind": "field",
            "name": name,
            "alias": alias,
            "arguments": arguments,
            "selection": sub,
        }

    def parse_arguments(self) -> dict[str, str]:
        self.expect("PUNCT", "(")
        args: dict[str, str] = {}
        while not _is(self.peek(), "PUNCT", ")"):
            if self.peek()[0] == "EOF":
                raise ValueError("unterminated argument list")
            key = self.expect("NAME")[1]
            self.expect("PUNCT", ":")
            args[key] = self.parse_value_raw()
        self.expect("PUNCT", ")")
        return args

    def parse_value_raw(self) -> str:
        start = self.peek()[2]
        self._consume_value()
        end = self.tokens[self.pos - 1][3]
        return self.text[start:end]

    def _consume_value(self) -> None:
        tok = self.peek()
        if tok[0] in ("STRING", "NUMBER", "NAME"):
            self.advance()
            return
        if _is(tok, "PUNCT", "$"):
            self.advance()
            self.expect("NAME")
            return
        if _is(tok, "PUNCT", "["):
            self.advance()
            while not _is(self.peek(), "PUNCT", "]"):
                if self.peek()[0] == "EOF":
                    raise ValueError("unterminated list value")
                self._consume_value()
            self.advance()
            return
        if _is(tok, "PUNCT", "{"):
            self.advance()
            while not _is(self.peek(), "PUNCT", "}"):
                if self.peek()[0] == "EOF":
                    raise ValueError("unterminated object value")
                self.expect("NAME")
                self.expect("PUNCT", ":")
                self._consume_value()
            self.advance()
            return
        raise ValueError(f"unexpected token {tok[1]!r} in value")

    def parse_variables(self) -> dict[str, str]:
        self.expect("PUNCT", "(")
        variables: dict[str, str] = {}
        while not _is(self.peek(), "PUNCT", ")"):
            if self.peek()[0] == "EOF":
                raise ValueError("unterminated variable definitions")
            self.expect("PUNCT", "$")
            name = self.expect("NAME")[1]
            self.expect("PUNCT", ":")
            variables[name] = self.parse_type()
        self.expect("PUNCT", ")")
        return variables

    def parse_type(self) -> str:
        if _is(self.peek(), "PUNCT", "["):
            self.advance()
            inner = self.parse_type()
            self.expect("PUNCT", "]")
            rendered = f"[{inner}]"
        else:
            rendered = self.expect("NAME")[1]
        if _is(self.peek(), "PUNCT", "!"):
            self.advance()
            rendered += "!"
        return rendered


def parse_query(text: str) -> dict[str, Any]:
    """解析查询文本，返回完整的文档 AST。

    支持 operation 关键字与名称、变量定义、任意层嵌套的选择集、字段
    别名、简单参数、具名片段定义和片段展开。空白不敏感。解析结果必须
    保留每一层 selection，不能只统计或只保留第一层字段。返回结构见模块
    docstring。文本不是合法查询时抛出 ValueError。
    """
    if not isinstance(text, str):
        raise ValueError("query text must be a string")
    parser = _Parser(text, _tokenize(text))
    operation: str | None = None
    op_name: str | None = None
    variables: dict[str, str] = {}
    selection: list[dict[str, Any]] | None = None
    fragments: dict[str, Any] = {}

    while parser.peek()[0] != "EOF":
        tok = parser.peek()
        if _is(tok, "PUNCT", "{"):
            if operation is not None:
                raise ValueError("multiple operations are not supported")
            operation = "query"
            selection = parser.parse_selection_set()
        elif tok[0] == "NAME" and tok[1] in ("query", "mutation", "subscription"):
            if operation is not None:
                raise ValueError("multiple operations are not supported")
            operation = parser.advance()[1]
            if parser.peek()[0] == "NAME":
                op_name = parser.advance()[1]
            if _is(parser.peek(), "PUNCT", "("):
                variables = parser.parse_variables()
            selection = parser.parse_selection_set()
        elif _is(tok, "NAME", "fragment"):
            parser.advance()
            frag_name = parser.expect("NAME")[1]
            on = parser.expect("NAME")[1]
            if on != "on":
                raise ValueError("fragment definition must use 'on <Type>'")
            type_condition = parser.expect("NAME")[1]
            frag_selection = parser.parse_selection_set()
            if frag_name in fragments:
                raise ValueError(f"duplicate fragment {frag_name!r}")
            fragments[frag_name] = {
                "kind": "fragment",
                "name": frag_name,
                "type_condition": type_condition,
                "selection": frag_selection,
            }
        else:
            raise ValueError(f"unexpected token {tok[1]!r} at document level")

    if operation is None or selection is None:
        raise ValueError("document does not contain an operation")
    return {
        "kind": "document",
        "operation": operation,
        "name": op_name,
        "variables": variables,
        "selection": selection,
        "fragments": fragments,
    }


def expand_fragments(query: dict[str, Any], fragments: dict[str, Any] | None) -> dict[str, Any]:
    """递归展开片段，返回新的文档 AST。

    fragments 是片段名到片段节点的映射；传入 None 时使用
    query["fragments"]。展开必须递归到任意深度，把每个 spread 节点替换
    为对应片段 selection 的深拷贝，并且不修改输入文档。同一片段被展开
    多次时每次都完整计入。

    片段直接或间接引用自身时必须抛出 ValueError；引用未定义的片段时
    同样抛出 ValueError。
    """
    frag_map = query.get("fragments", {}) if fragments is None else fragments

    def expand_selection(
        selection: list[dict[str, Any]], stack: frozenset[str]
    ) -> list[dict[str, Any]]:
        expanded: list[dict[str, Any]] = []
        for node in selection:
            kind = node.get("kind")
            if kind == "spread":
                name = node["name"]
                if name not in frag_map:
                    raise ValueError(f"undefined fragment {name!r}")
                if name in stack:
                    raise ValueError(f"fragment cycle detected at {name!r}")
                frag = frag_map[name]
                expanded.extend(
                    expand_selection(frag["selection"], stack | {name})
                )
            elif kind == "field":
                expanded.append(
                    {
                        "kind": "field",
                        "name": node["name"],
                        "alias": node["alias"],
                        "arguments": dict(node["arguments"]),
                        "selection": expand_selection(node["selection"], stack),
                    }
                )
            else:
                raise ValueError(f"unknown node kind {kind!r}")
        return expanded

    return {
        "kind": "document",
        "operation": query["operation"],
        "name": query["name"],
        "variables": dict(query["variables"]),
        "selection": expand_selection(query["selection"], frozenset()),
        "fragments": dict(frag_map),
    }


def depth_of(node: dict[str, Any]) -> int:
    """返回文档或字段节点的最大嵌套深度。

    document 的深度取顶层字段深度的最大值，空选择集深度为 0。字段节点
    自身计 1 层，存在子选择集时再加子节点深度的最大值。别名只是同层
    字段的另一个名字，不计入深度。未展开的 spread 节点抛出 ValueError。
    深度使用整数精确比较，容差为 0。
    """
    kind = node.get("kind")
    if kind == "document":
        if not node["selection"]:
            return 0
        return max(depth_of(child) for child in node["selection"])
    if kind == "field":
        if not node["selection"]:
            return 1
        return 1 + max(depth_of(child) for child in node["selection"])
    if kind == "spread":
        raise ValueError("unexpanded fragment spread; call expand_fragments first")
    raise ValueError(f"unknown node kind {kind!r}")


def field_count(node: dict[str, Any], alias_aware: bool) -> int:
    """返回字段节点的出现次数。

    node 可以是 document 或字段节点；document 统计顶层选择集。alias_aware
    为 True 时按实际请求计数：每个字段节点算一次，同一底层字段使用多个
    别名就按别名数量累计，嵌套层级的字段逐层累计。alias_aware 为 False
    仅保留旧版对照语义：每个选择集内按字段名去重，跨选择集仍分别计数；
    安全判定必须使用 True。

    片段定义本身不计数，只有展开后的字段才计数。未展开的 spread 节点
    抛出 ValueError。计数使用整数精确比较，容差为 0。
    """
    kind = node.get("kind")
    if kind == "document":
        return _count_selection(node["selection"], alias_aware)
    if kind == "field":
        return 1 + _count_selection(node["selection"], alias_aware)
    if kind == "spread":
        raise ValueError("unexpanded fragment spread; call expand_fragments first")
    raise ValueError(f"unknown node kind {kind!r}")


def _count_selection(selection: list[dict[str, Any]], alias_aware: bool) -> int:
    if alias_aware:
        total = 0
        for node in selection:
            kind = node.get("kind")
            if kind == "field":
                total += 1 + _count_selection(node["selection"], True)
            elif kind == "spread":
                raise ValueError(
                    "unexpanded fragment spread; call expand_fragments first"
                )
            else:
                raise ValueError(f"unknown node kind {kind!r}")
        return total
    seen: set[str] = set()
    total = 0
    for node in selection:
        kind = node.get("kind")
        if kind == "field":
            if node["name"] in seen:
                continue
            seen.add(node["name"])
            total += 1 + _count_selection(node["selection"], False)
        elif kind == "spread":
            raise ValueError(
                "unexpanded fragment spread; call expand_fragments first"
            )
        else:
            raise ValueError(f"unknown node kind {kind!r}")
    return total


def cost_of(node: dict[str, Any], weights: dict[str, Any]) -> float:
    """按权重计算查询成本。

    weights 支持两种形式。结构化形式包含 fields（字段名到基础权重）、
    list_fields（列表字段名集合）和 list_multiplier（列表放大倍数）。
    没有 fields 键时，整个映射视为字段名到基础权重的简写，此时没有
    列表字段，放大倍数为 1.0。

    每个字段先取基础权重，缺省 1.0。字段名在 list_fields 中时，自身
    权重乘以 list_multiplier，其整个子树的成本也乘以 list_multiplier。
    同一底层字段的每个别名出现都独立计费。document 的成本是顶层字段
    成本之和，空查询成本为 0.0。返回浮点总成本，比较容差为 0。
    """
    weights = weights or {}
    if "fields" in weights:
        field_weights = weights.get("fields") or {}
        list_fields = set(weights.get("list_fields") or ())
        list_multiplier = float(weights.get("list_multiplier", 1.0))
    else:
        field_weights = weights
        list_fields = set()
        list_multiplier = 1.0

    def field_cost(field: dict[str, Any]) -> float:
        base = float(field_weights.get(field["name"], 1.0))
        subtotal = base
        for child in field["selection"]:
            kind = child.get("kind")
            if kind == "field":
                subtotal += field_cost(child)
            elif kind == "spread":
                raise ValueError(
                    "unexpanded fragment spread; call expand_fragments first"
                )
            else:
                raise ValueError(f"unknown node kind {kind!r}")
        if field["name"] in list_fields:
            subtotal *= list_multiplier
        return subtotal

    kind = node.get("kind")
    if kind == "document":
        return float(sum(field_cost(child) for child in node["selection"]))
    if kind == "field":
        return float(field_cost(node))
    if kind == "spread":
        raise ValueError("unexpanded fragment spread; call expand_fragments first")
    raise ValueError(f"unknown node kind {kind!r}")


def check_limits(
    cost: float,
    depth: int,
    fields: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """根据策略判定成本、深度、字段数三项上限。

    policy 只读取 max_cost、max_depth、max_fields；值为 None 或缺失
    表示该项不限制。边界是包含的：恰好等于上限仍然放行，只有严格超过
    才拒绝。三项独立判定，任意一项超限都必须拒绝，不能因为其他项未超限
    而放行。

    返回 {"allowed": bool, "violations": list[str], "cost": float,
    "depth": int, "fields": int}。violations 按 cost、depth、fields
    的固定顺序列出超限项，全部通过时为空列表。所有阈值比较容差为 0。
    """
    violations: list[str] = []
    max_cost = policy.get("max_cost")
    if max_cost is not None and cost > max_cost:
        violations.append("cost")
    max_depth = policy.get("max_depth")
    if max_depth is not None and depth > max_depth:
        violations.append("depth")
    max_fields = policy.get("max_fields")
    if max_fields is not None and fields > max_fields:
        violations.append("fields")
    return {
        "allowed": not violations,
        "violations": violations,
        "cost": cost,
        "depth": depth,
        "fields": fields,
    }


def validate_policy(policy: dict[str, Any]) -> list[str]:
    """校验策略完整性，返回问题代码列表。

    必须检查三项：缺少深度上限返回 missing_depth_limit，缺少成本上限
    返回 missing_cost_limit，未限制片段数返回 missing_fragment_limit。
    按上述顺序返回；策略完整时返回空列表。

    max_depth 与 max_cost 必须是正数，max_fragments 必须是非负数；
    缺失、None 或非法值都视为缺少对应上限。
    """
    problems: list[str] = []

    def is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    max_depth = policy.get("max_depth")
    if not is_number(max_depth) or max_depth <= 0:
        problems.append("missing_depth_limit")
    max_cost = policy.get("max_cost")
    if not is_number(max_cost) or max_cost <= 0:
        problems.append("missing_cost_limit")
    max_fragments = policy.get("max_fragments")
    if not is_number(max_fragments) or max_fragments < 0:
        problems.append("missing_fragment_limit")
    return problems


def query_batch(queries: list[str], policy: dict[str, Any]) -> dict[str, Any]:
    """独立评估一批查询文本，返回逐条结果与汇总。

    queries 是查询文本序列。每一条都必须重新解析、展开、计算深度、计数
    和计算成本，禁止在查询之间复用解析结果、片段表或计数缓存，否则后
    一个查询会命中前一个查询的结构。

    policy 中的 max_cost、max_depth、max_fields 传给 check_limits，
    cost_weights 传给 cost_of（缺省为空映射）。返回
    {"allowed": bool, "total_cost": float, "results": [单条评估, ...]}，
    每条评估与 check_limits 的返回值完全一致。整批 allowed 仅当每条
    查询都放行时为 True。所有比较容差为 0。
    """
    weights = policy.get("cost_weights", {})
    results: list[dict[str, Any]] = []
    for text in queries:
        document = parse_query(text)
        expanded = expand_fragments(document, document["fragments"])
        depth = depth_of(expanded)
        fields = field_count(expanded, alias_aware=True)
        cost = cost_of(expanded, weights)
        results.append(check_limits(cost, depth, fields, policy))
    return {
        "allowed": all(item["allowed"] for item in results),
        "total_cost": sum(item["cost"] for item in results),
        "results": results,
    }
