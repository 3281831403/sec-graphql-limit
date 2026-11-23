"""GraphQL 查询限流核心契约。

本模块只依赖标准库。八个公开函数当前全部是桩，调用即抛出
NotImplementedError，等待按 README 中的顺序逐项实现。

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


def parse_query(text: str) -> dict[str, Any]:
    """解析查询文本，返回完整的文档 AST。

    支持 operation 关键字与名称、变量定义、任意层嵌套的选择集、字段
    别名、简单参数、具名片段定义和片段展开。空白不敏感。解析结果必须
    保留每一层 selection，不能只统计或只保留第一层字段。返回结构见模块
    docstring。文本不是合法查询时抛出 ValueError。
    """
    raise NotImplementedError


def expand_fragments(query: dict[str, Any], fragments: dict[str, Any] | None) -> dict[str, Any]:
    """递归展开片段，返回新的文档 AST。

    fragments 是片段名到片段节点的映射；传入 None 时使用
    query["fragments"]。展开必须递归到任意深度，把每个 spread 节点替换
    为对应片段 selection 的深拷贝，并且不修改输入文档。同一片段被展开
    多次时每次都完整计入。

    片段直接或间接引用自身时必须抛出 ValueError；引用未定义的片段时
    同样抛出 ValueError。
    """
    raise NotImplementedError


def depth_of(node: dict[str, Any]) -> int:
    """返回文档或字段节点的最大嵌套深度。

    document 的深度取顶层字段深度的最大值，空选择集深度为 0。字段节点
    自身计 1 层，存在子选择集时再加子节点深度的最大值。别名只是同层
    字段的另一个名字，不计入深度。未展开的 spread 节点抛出 ValueError。
    深度使用整数精确比较，容差为 0。
    """
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def validate_policy(policy: dict[str, Any]) -> list[str]:
    """校验策略完整性，返回问题代码列表。

    必须检查三项：缺少深度上限返回 missing_depth_limit，缺少成本上限
    返回 missing_cost_limit，未限制片段数返回 missing_fragment_limit。
    按上述顺序返回；策略完整时返回空列表。

    max_depth 与 max_cost 必须是正数，max_fragments 必须是非负数；
    缺失、None 或非法值都视为缺少对应上限。
    """
    raise NotImplementedError


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
    raise NotImplementedError