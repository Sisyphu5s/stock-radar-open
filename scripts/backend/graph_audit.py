"""图审计器:调 graph_build 构建调用图,跑规则 B1~B4,对照白名单输出 [PASS]/[FAIL]/[WARN]。

规则:
- B1 分层环:模块依赖图 SCC(Tarjan),同一 SCC 跨层 → FAIL;api 层直穿 storage 层 → FAIL
- B2 死代码:callers 为空、无豁免、且全项目文本粗筛确认无引用的函数 → WARN(不 FAIL)
- B3 热路径:热点 GET 端点沿 routes.callees BFS(≤4 跳)命中 sessionlocal → FAIL
- B4 铁律标注:marks 中 raw_in/os_exit/shared_session/global_write → FAIL

白名单 scripts/backend/graph-whitelist.json(缺省空):
  { "b1_edges": [], "b2_dead": [], "b3_hot": [], "b4_marks": [] }
差集 = 新增违例。任一 FAIL → exit 1。规则函数 run_bX(graph, whitelist) 可独立 import 测试。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPTS_DIR.parent.parent / "backend" / "app"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from graph_build import build  # noqa: E402

# ---- 常量 ----
LAYER_DIRS = ("api", "core", "storage", "lib")
HOT_PATH_FRAGMENTS = (
    "/market/snapshot",
    "/market/quote",
    "/signals",
    "/market_stream",
    "/search",
    "/screener",
    "/experiments",
)
MAX_BFS_HOPS = 4
MARK_HOT_KINDS = ("raw_in", "os_exit", "shared_session", "global_write")

EMPTY_WHITELIST = {"b1_edges": [], "b2_dead": [], "b3_hot": [], "b4_marks": []}


def load_whitelist(path: Path | None) -> dict:
    """读白名单;文件缺失/损坏 → 空白名单(缺省空,不阻断审计)。"""
    if path is None or not Path(path).exists():
        return dict(EMPTY_WHITELIST)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        wl = dict(EMPTY_WHITELIST)
        for key in wl:
            wl[key] = list(data.get(key, []))
        return wl
    except (json.JSONDecodeError, OSError):
        return dict(EMPTY_WHITELIST)


def layer_of(rel: str) -> str | None:
    """文件路径第一段(api/core/storage/lib),不在分层内返回 None。"""
    head = rel.split("/", 1)[0]
    return head if head in LAYER_DIRS else None


# ---- B1 分层环(Tarjan SCC 自实现)----
def _tarjan_sccs(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 强连通分量:返回 size>=2 的 SCC 列表(单点 SCC 与无环无关,不返回)。"""
    index_counter = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        index[v] = index_counter
        low[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(comp)

    for v in nodes:
        if v not in index:
            strongconnect(v)
    return sccs


def run_b1(graph: dict, whitelist: dict | None = None) -> list[dict]:
    """B1 分层环:跨层 SCC 与 api→storage 直穿。返回 issues(level=FAIL)。"""
    wl = whitelist or EMPTY_WHITELIST
    wl_edges = set(wl.get("b1_edges", []))
    modules = graph.get("modules", {})
    # 依赖边:file → imports(已解析到具体文件路径)
    edges: dict[str, set[str]] = {}
    nodes: list[str] = []
    for rel, info in modules.items():
        nodes.append(rel)
        edges.setdefault(rel, set()).update(info.get("imports", []))
    issues: list[dict] = []
    # 1) 跨层 SCC
    for comp in _tarjan_sccs(nodes, edges):
        layers = {layer_of(f) for f in comp if layer_of(f)}
        if len(layers) >= 2:
            ckey = "<->".join(sorted(comp))
            if ckey in wl_edges:
                continue
            issues.append(
                {
                    "rule": "B1",
                    "level": "FAIL",
                    "key": ckey,
                    "evidence": f"跨层环 {sorted(comp)} (层: {sorted(layers)})",
                }
            )
    # 2) api 层直穿 storage 层
    seen: set[str] = set()
    for rel, info in modules.items():
        if layer_of(rel) != "api":
            continue
        for imp in info.get("imports", []):
            if layer_of(imp) == "storage":
                ekey = f"{rel}->{imp}"
                if ekey in wl_edges or ekey in seen:
                    continue
                seen.add(ekey)
                issues.append(
                    {
                        "rule": "B1",
                        "level": "FAIL",
                        "key": ekey,
                        "evidence": f"api 层直穿 storage: {rel} → {imp}",
                    }
                )
    return issues


# ---- B2 死代码 ----
def _text_referenced(
    app_dir: Path, file: str, name: str, files_text: dict[str, str]
) -> bool:
    """文本粗筛:函数裸名在项目源码(其他文件全文 + 自身文件非 def 行)出现 = 有动态/跨模块/类内调用,
    排除出死代码候选。纯 AST 解析不了的调用形态(self.method()/getattr/注册表字符串引用)由此兜底。"""
    method = name.split(".")[-1]
    if not method or method.startswith("__"):
        return True  # 魔术方法(pydantic hooks 等)由框架调用,不做死代码判定
    pat = re.compile(rf"\b{re.escape(method)}\b")
    for f, text in files_text.items():
        if f != file:
            if pat.search(text):
                return True
            continue
        # 自身文件:排除 def/class 定义行
        for line in text.splitlines():
            if pat.search(line) and not re.match(
                rf"\s*(async\s+)?def\s+{re.escape(method)}\b", line
            ):
                return True
    return False


def run_b2(
    graph: dict, whitelist: dict | None = None, app_dir: Path | None = None
) -> list[dict]:
    """B2 死代码:callers 为空、无豁免、且文本粗筛确认全项目无引用的函数 → WARN(不 FAIL)。
    豁免:路由 handler / 注册实参引用 / plugins / __init__ / 类内私有方法(_ 段) /
    pydantic model_* 钩子 / 魔术方法(框架反射调用,不做死代码判定)。
    app_dir 可注入(测试用假项目目录),默认真实 backend/app。"""
    wl = whitelist or EMPTY_WHITELIST
    wl_dead = set(wl.get("b2_dead", []))
    calls = graph.get("calls", {})
    routes = graph.get("routes", [])
    app_dir = app_dir or APP_DIR
    # 全项目源码文本(粗筛用,一次读入);key 与 graph 内 file 同格式(相对 app_dir)
    files_text: dict[str, str] = {}
    for p in app_dir.rglob("*.py"):
        try:
            files_text[str(p.relative_to(app_dir))] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    issues: list[dict] = []
    route_keys = {f"{r['file']}::{r['handler']}" for r in routes}
    for key, info in calls.items():
        if info.get("callers"):
            continue
        file, _, name = key.rpartition("::")
        # 豁免:路由 handler / 注册实参引用 / plugins.py / __init__.py / _ 开头私有
        if key in route_keys:
            continue
        if info.get("used_as_arg"):
            continue
        if file.startswith("app/core/plugins.py"):
            continue
        if file.endswith("/__init__.py"):
            continue
        method = name.split(".")[-1]
        if method.startswith("_") or method.startswith("model_"):
            # 类内私有方法 / pydantic model_* 生命周期钩子:框架反射调用,不做死代码判定
            continue
        if key in wl_dead:
            continue
        # 文本粗筛:其他文件或自身文件(非定义行)出现裸名 → 非死代码
        if _text_referenced(app_dir, file, name, files_text):
            continue
        issues.append(
            {
                "rule": "B2",
                "level": "WARN",
                "key": key,
                "evidence": f"无调用者且全项目无文本引用: {key} (file: {file})",
            }
        )
    return issues


# ---- B3 热路径 ----
def _route_node_key(route: dict) -> str:
    return f"{route['file']}::{route['handler']}"


def run_b3(graph: dict, whitelist: dict | None = None) -> list[dict]:
    """B3 热路径:热点 GET 端点沿 callees BFS(≤4 跳)命中 sessionlocal → FAIL。

    sessionlocal 命中判定:marks 中 kind=sessionlocal 的 callee 限定名与 BFS 路径节点重合。
    证据输出完整路径链。
    """
    wl = whitelist or EMPTY_WHITELIST
    wl_hot = set(wl.get("b3_hot", []))
    routes = graph.get("routes", [])
    calls = graph.get("calls", {})
    # callee 限定名 → 节点 key 反查(BFS 展开用)
    key_by_qual: dict[str, str] = {}
    for key, info in calls.items():
        file, _, name = key.rpartition("::")
        key_by_qual[f"{file.replace('/', '.')[:-3]}.{name}"] = key
    # sessionlocal 调用点限定名集合
    hot_callees = {
        m["callee"] for m in graph.get("marks", []) if m["kind"] == "sessionlocal"
    }
    issues: list[dict] = []
    seen_routes: set[str] = set()
    for route in routes:
        if route["method"] != "GET":
            continue
        if not any(frag in route["path"] for frag in HOT_PATH_FRAGMENTS):
            continue
        if route["path"] in wl_hot or route["path"] in seen_routes:
            continue
        seen_routes.add(route["path"])
        start = _route_node_key(route)
        path = _bfs_hot_path(start, calls, key_by_qual, hot_callees)
        if path is not None:
            issues.append(
                {
                    "rule": "B3",
                    "level": "FAIL",
                    "key": route["path"],
                    "evidence": f"热路径 {route['method']} {route['path']} 命中 SessionLocal: {' → '.join(path)}",
                }
            )
    return issues


def _bfs_hot_path(
    start: str, calls: dict, key_by_qual: dict, hot_callees: set
) -> list | None:
    """从 handler 节点 BFS 至多 4 跳,返回命中 sessionlocal 的完整路径;未命中返回 None。"""
    # 队列元素:(节点 key, 已走路径[限定名列表])
    frontier = [(start, [start])]
    for _hop in range(MAX_BFS_HOPS):
        next_frontier = []
        for key, path in frontier:
            node = calls.get(key)
            for callee in node.get("callees", []):
                if callee in hot_callees:
                    return path + [callee]
                child = key_by_qual.get(callee)
                if child and child not in path:
                    next_frontier.append((child, path + [child]))
        frontier = next_frontier
        if not frontier:
            break
    return None


# ---- B4 铁律标注 ----
def run_b4(graph: dict, whitelist: dict | None = None) -> list[dict]:
    """B4 铁律标注:marks 中 raw_in/os_exit/shared_session/global_write → FAIL。"""
    wl = whitelist or EMPTY_WHITELIST
    wl_marks = set(wl.get("b4_marks", []))
    issues: list[dict] = []
    for mark in graph.get("marks", []):
        if mark["kind"] not in MARK_HOT_KINDS:
            continue
        key = f"{mark['file']}:{mark['line']}"
        if key in wl_marks:
            continue
        issues.append(
            {
                "rule": "B4",
                "level": "FAIL",
                "key": key,
                "evidence": f"{mark['kind']} @ {mark['file']}:{mark['line']} — {mark['detail']}",
            }
        )
    return issues


# ---- 输出 ----
MAX_DETAIL_ROWS = 30  # WARN 量大的规则(B2 死代码)明细截断,防刷屏


def _print_rules(issues_by_rule: dict[str, list[dict]]) -> tuple[int, int]:
    """输出 [PASS]/[FAIL]/[WARN] 行;返回 (fail_count, warn_count)。"""
    fails = warns = 0
    for rule in ("B1", "B2", "B3", "B4"):
        issues = issues_by_rule.get(rule, [])
        if not issues:
            print(f"[PASS] {rule} 未发现违例")
            continue
        shown = issues if rule != "B2" else issues[:MAX_DETAIL_ROWS]
        for issue in shown:
            tag = issue["level"]
            if tag == "FAIL":
                fails += 1
            else:
                warns += 1
            print(f"[{tag}] {rule} {issue['key']}\n      证据: {issue['evidence']}")
        if rule == "B2" and len(issues) > MAX_DETAIL_ROWS:
            warns += len(issues) - MAX_DETAIL_ROWS
            print(
                f"[WARN] B2 明细截断: 另 {len(issues) - MAX_DETAIL_ROWS} 处死代码见 --graph 全量 JSON"
            )
    return fails, warns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后端图审计器(规则 B1~B4)")
    parser.add_argument(
        "--root",
        default=str(SCRIPTS_DIR.parent.parent / "backend" / "app"),
        help="扫描根目录(默认 backend/app)",
    )
    parser.add_argument(
        "--whitelist",
        default=str(SCRIPTS_DIR / "graph-whitelist.json"),
        help="白名单 JSON 路径(缺省空)",
    )
    parser.add_argument("--graph", default=None, help="复用已生成的图 JSON(跳过构建)")
    args = parser.parse_args(argv)

    if args.graph:
        graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    else:
        graph = build(Path(args.root))

    whitelist = load_whitelist(Path(args.whitelist))
    issues_by_rule = {
        "B1": run_b1(graph, whitelist),
        "B2": run_b2(graph, whitelist),
        "B3": run_b3(graph, whitelist),
        "B4": run_b4(graph, whitelist),
    }
    fails, warns = _print_rules(issues_by_rule)
    stats = graph.get("stats", {})
    print(
        f"\nScore: FAIL={fails} WARN={warns} "
        f"(图: 文件 {stats.get('files', 0)} / 路由 {stats.get('routes', 0)} / "
        f"函数 {stats.get('functions', 0)} / 标记 {stats.get('marks', 0)})"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
