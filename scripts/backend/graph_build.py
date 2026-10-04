"""图构建器:零视觉、零执行的静态分析——用 ast 遍历 backend/app/**/*.py,构建调用图。

输出 JSON(默认 stdout,`--out <path>` 写文件):
- modules:  每文件 → imports(解析到具体文件路径)、functions(定义的顶层函数/类方法)
- routes:   每路由 {path, method, handler, file, callees[]}
- calls:    每函数 → callees(限定名集合)+ 反向 callers
- marks:    标记点数组 {kind, file, line, detail}
- stats:    数值统计(文件数/路由数/函数数/marks 数)

全部基于标准库 ast,零第三方依赖,不执行任何业务代码。
可 import 复用:build(root_dir) -> dict。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# ---- 常量(可变性显式化)----
# 分层目录:依赖环/直穿规则按文件路径第一段判定层级
LAYER_DIRS = ("api", "core", "storage", "lib")
# 模块级可变容器修改的方法名(global_write 标记)
MUTABLE_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "update",
        "add",
        "discard",
        "setdefault",
        "put",
    }
)
# 分批上下文提示词:函数体内出现这些调用名 → 视为 SQL 已分批,raw_in 豁免
BATCH_HINTS = ("chunk", "batch", "executemany", "chunks")
# 路由装饰器可识别的方法(router/app 打头的 Attribute)
ROUTE_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})


def walk_files(root: Path) -> list[Path]:
    """递归收集 root 下全部 .py 文件,排序保证输出稳定。"""
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def rel_path(root: Path, path: Path) -> str:
    """文件相对 root 的路径(统一用 / 分隔,如 app/api/market.py)。"""
    return path.relative_to(root).as_posix()


def module_qualname(rel: str) -> str:
    """相对路径 → 模块限定名:app/api/market.py → app.api.market。"""
    return rel[:-3].replace("/", ".")


def package_of(rel: str) -> str:
    """文件所在包(不含文件名):app/api/market.py → app.api。"""
    parts = rel.split("/")
    return ".".join(parts[:-1])


class ImportResolver:
    """import 语句 → (引入名, 目标模块限定名) 解析器。

    依托全项目模块索引(module_index:限定名 → 相对路径)做"尽力解析":
    - `import app.storage.db` → 绑定 app → app.storage.db(候选)
    - `from app.core import sources` → 候选 [app.core.sources] 命中索引 → 绑定 sources → app.core.sources
    - `from ..storage.db import get_db` → 绑定 get_db → app.storage.db
    - `from . import codes` → 绑定 codes → 当前包.codes
    - 目标不在索引(第三方/外部)→ 绑定名不进入映射(调用解析时自然跳过)
    """

    def __init__(self, module_index: dict[str, str]):
        self.module_index = module_index  # 限定名 → 相对路径

    def resolve_module(
        self, node: ast.Import | ast.ImportFrom, rel: str
    ) -> list[tuple[str, str]]:
        """返回 [(引入名, 目标模块限定名), ...];解析不到的返回空列表。"""
        pkg_parts = package_of(rel).split(".") if rel != "app/__init__.py" else []
        out: list[tuple[str, str]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                # `import a.b.c` 只绑定首段 a;带 asname 则绑定 asname
                name = alias.asname or mod.split(".")[0]
                out.append((name, mod))
        else:  # ImportFrom
            level = node.level
            base = node.module or ""
            if base.startswith("."):
                # 相对导入:`..storage.db` → 包剥 level 段后接剩余
                dots = len(base) - len(base.lstrip("."))
                rest = base.lstrip(".")
                keep = len(pkg_parts) - level if len(pkg_parts) >= level else 0
                base_mod = ".".join(pkg_parts[:keep])
                if rest:
                    base_mod = f"{base_mod}.{rest}" if base_mod else rest
            elif base == "":
                # from . import x(module 为 None 时与相对导入同路,已在上面处理)
                base_mod = ".".join(pkg_parts)
            else:
                base_mod = base
            for alias in node.names:
                name = alias.asname or alias.name
                if node.module is None:
                    # from . import codes:names 是子模块,候选 = 包.codes
                    candidates = [f"{base_mod}.{name}"]
                else:
                    # from X import y:先假设 y 是子模块,命中索引才成立,否则是对象
                    candidates = [f"{base_mod}.{name}", base_mod]
                for cand in candidates:
                    if cand in self.module_index or cand == base_mod:
                        out.append((name, cand))
                        break
        return out


class ModuleAnalyzer:
    """单文件的 AST 分析器:收集模块级名称/import 映射/函数节点/路由/标记点。"""

    def __init__(self, rel: str, tree: ast.Module, resolver: ImportResolver):
        self.rel = rel  # 相对路径 app/api/market.py
        self.qualname = module_qualname(rel)
        self.tree = tree
        self.resolver = resolver
        self.parent_map: dict[ast.AST, ast.AST] = {}

        self.import_map: dict[str, str] = {}  # 引入名 → 目标模块限定名
        self.local_defs: set[str] = set()  # 模块级可调用名(顶层函数/类/模块变量)
        self.funcs: dict[str, dict] = {}  # key(file::name) → 函数元信息
        self.used_as_arg: set[str] = set()  # 作为实参被引用的函数 key(注册表等)
        self.routes: list[dict] = []
        self.marks: list[dict] = []
        self.imports: list[str] = []  # 解析到的目标文件路径列表
        self.router_prefix: str = ""  # router = APIRouter(prefix=...) 的 prefix
        # global_write 收敛(两阶段):模块级容器字面量赋值只是候选——
        # 仅当该容器名在函数体内被实际修改(下标写/append 等/global 声明)才标记。
        # 纯常量表(只读 dict/list)与初始化两段式(模块级重新赋值)不是共享可变状态,不报。
        self._mutated_globals: set[str] = set()
        self._global_containers: set[str] = set()  # 模块级容器字面量赋值的名字
        self._container_assigns: list[dict] = []  # {name, line, detail}

    # ---- 遍历 ----
    def analyze(self) -> None:
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent_map[child] = node
        # 第一遍:模块级名称 + import 映射 + 函数清单
        for node in self.tree.body:
            self._collect_module_level(node)
        # 第二遍:函数体调用解析 + 路由 + 标记点
        for node in self.tree.body:
            self._scan_module_level(node)
        # global_write 两阶段过滤:候选容器赋值中,仅被实际修改的才标记
        for cand in self._container_assigns:
            if cand["name"] in self._mutated_globals:
                self.marks.append(
                    {
                        "kind": "global_write",
                        "file": self.rel,
                        "line": cand["line"],
                        "detail": f"模块级可变容器被修改: {cand['detail']}",
                    }
                )

    def _collect_module_level(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = self._func_key(node)
            self.local_defs.add(node.name)
            self.funcs[key] = {
                "file": self.rel,
                "name": node.name,
                "line": node.lineno,
                "is_method": self._is_method(node),
            }
        elif isinstance(node, ast.ClassDef):
            self.local_defs.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = self._func_key(item, cls=node.name)
                    self.funcs[key] = {
                        "file": self.rel,
                        "name": f"{node.name}.{item.name}",
                        "line": item.lineno,
                        "is_method": True,
                    }
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, mod in self.resolver.resolve_module(node, self.rel):
                if name not in self.import_map:
                    self.import_map[name] = mod
                if mod in self.resolver.module_index:
                    self.imports.append(self.resolver.module_index[mod])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in self._assign_targets(node):
                if isinstance(target, ast.Name):
                    self.local_defs.add(target.id)
                    # router = APIRouter(prefix=...) → 记录 prefix(路由全路径拼接用)
                    if (
                        target.id == "router"
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "APIRouter"
                    ):
                        for kw in node.value.keywords:
                            if (
                                kw.arg == "prefix"
                                and isinstance(kw.value, ast.Constant)
                                and isinstance(kw.value.value, str)
                            ):
                                self.router_prefix = kw.value.value
            # 模块级容器名提前收集(第一遍,顺序无关):函数体内修改检测依赖
            if self._is_container_literal(node.value):
                for t in self._assign_targets(node):
                    if isinstance(t, ast.Name):
                        self._global_containers.add(t.id)

    def _func_key(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, cls: str | None = None
    ) -> str:
        name = f"{cls}.{node.name}" if cls else node.name
        return f"{self.rel}::{name}"

    def _is_method(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return isinstance(self.parent_map.get(node), ast.ClassDef)

    def _assign_targets(self, node: ast.Assign | ast.AnnAssign):
        if isinstance(node, ast.AnnAssign) and node.target:
            yield node.target
        else:
            for t in node.targets:
                yield t

    # ---- 模块级语句扫描(路由/标记) ----
    def _scan_module_level(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = self._func_key(node)
            self._scan_route(node, key)
            self._scan_function_body(node, key)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = self._func_key(item, cls=node.name)
                    self._scan_function_body(item, key)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            self._scan_module_assign(node)
        elif isinstance(node, ast.Expr):
            self._scan_module_expr(node)

    # ---- 路由 ----
    def _scan_route(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, key: str
    ) -> None:
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute) or not isinstance(
                func.value, ast.Name
            ):
                continue
            base, attr = func.value.id, func.attr
            if base not in ("router", "app") or attr.lower() not in ROUTE_METHODS:
                continue
            path = self._const_path(dec.args[0]) if dec.args else None
            if path is None:
                continue
            # 拼接 router prefix:APIRouter(prefix='/market') + @router.get('/snapshot')
            # → '/market/snapshot'(与 openapi 全路径一致,B3 热点匹配依赖)
            if self.router_prefix:
                path = self.router_prefix.rstrip("/") + "/" + path.lstrip("/")
            self.routes.append(
                {
                    "path": path,
                    "method": attr.upper(),
                    "handler": node.name,
                    "file": self.rel,
                    "callees": [],
                }
            )
            return  # 一个函数只认第一个路由装饰器

    @staticmethod
    def _const_path(arg: ast.expr) -> str | None:
        """装饰器路径实参 → 字符串;f-string 拼常量片段,FormattedValue 用 {} 占位。"""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.JoinedStr):
            parts = []
            for v in arg.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{}")
            return "".join(parts)
        return None

    # ---- 函数体调用解析 ----
    def _scan_function_body(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, key: str
    ) -> None:
        shadow = self._collect_shadow(node)
        callees: list[str] = []
        seen: set[str] = set()
        has_batch_ctx = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = self._call_func_text(sub.func)
                if name and any(h in name.lower() for h in BATCH_HINTS):
                    has_batch_ctx = True
                q = self._resolve_call(sub, shadow)
                if q and q not in seen:
                    seen.add(q)
                    callees.append(q)
                # 实参引用收集:作为实参的函数名视为"被引用"(注册表/回调等)
                for a in sub.args:
                    if isinstance(a, ast.Name) and a.id in self.local_defs:
                        arg_key = f"{self.rel}::{a.id}"
                        if arg_key in self.funcs:
                            self.used_as_arg.add(arg_key)
            elif isinstance(sub, ast.For):
                tname = self._for_target_name(sub)
                if tname and any(h in tname.lower() for h in BATCH_HINTS):
                    has_batch_ctx = True
        self.funcs[key]["callees"] = callees
        # 函数体内对模块级容器的运行时修改(global_write 两阶段收敛的一部分):
        # 下标写 CACHE["k"]=v / 方法调用 CACHE.append() / global 声明,且基名是
        # 本模块的模块级容器、未被函数局部遮蔽 → 计入 _mutated_globals
        for sub in ast.walk(node):
            base = None
            if isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store):
                base = self._subscript_base(sub)
            elif (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
            ):
                if sub.func.attr in MUTABLE_METHODS:
                    base = sub.func.value.id
            elif isinstance(sub, ast.Global):
                for gname in sub.names:
                    if gname in self._global_containers:
                        self._mutated_globals.add(gname)
                continue
            if base and base in self._global_containers and base not in shadow:
                self._mutated_globals.add(base)
        # 字符串 SQL 构造(raw_in):同一函数内无分批上下文才标记(按行去重,
        # f-string 的 JoinedStr 与内部 Constant 会重复命中同一位置)
        if not has_batch_ctx:
            seen_raw: set[int] = set()
            for sub in ast.walk(node):
                for text, ln in self._string_literals(sub):
                    if " in (" in text.lower() and ln not in seen_raw:
                        seen_raw.add(ln)
                        self.marks.append(
                            {
                                "kind": "raw_in",
                                "file": self.rel,
                                "line": ln,
                                "detail": "字符串 SQL 含 ' IN (',且同函数内未见分批调用上下文",
                            }
                        )
        # 函数内直接调用 SessionLocal(sessionlocal 标记):
        # 限定名末段为 SessionLocal 且当前文件非定义文件(storage/db.py)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                q = self._resolve_call(sub, shadow)
                if (
                    q
                    and q.endswith(".SessionLocal")
                    and not self.rel.endswith("storage/db.py")
                ):
                    self.marks.append(
                        {
                            "kind": "sessionlocal",
                            "file": self.rel,
                            "line": sub.lineno,
                            "callee": q,
                            "func": node.name,
                            "detail": f"直接调用 SessionLocal({q})",
                        }
                    )
                # os_exit:函数体内 os._exit((任务卡未限定模块级)
                if self._is_os_exit(sub.func):
                    self.marks.append(
                        {
                            "kind": "os_exit",
                            "file": self.rel,
                            "line": sub.lineno,
                            "detail": "调用 os._exit(",
                        }
                    )

    def _collect_shadow(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        """函数体内遮蔽名(参数/局部赋值/循环变量等),这些名字不参与跨模块解析。"""
        shadow = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if node.args.vararg:
            shadow.add(node.args.vararg.arg)
        if node.args.kwarg:
            shadow.add(node.args.kwarg.arg)
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                for t in self._assign_targets(sub):
                    if isinstance(t, ast.Name):
                        shadow.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for el in t.elts:
                            if isinstance(el, ast.Name):
                                shadow.add(el.id)
            elif isinstance(sub, (ast.For, ast.AsyncFor)):
                tn = self._for_target_name(sub)
                if tn:
                    shadow.add(tn)
            elif isinstance(sub, ast.comprehension):
                if isinstance(sub.target, ast.Name):
                    shadow.add(sub.target.id)
            elif isinstance(sub, ast.withitem) and sub.optional_vars:
                if isinstance(sub.optional_vars, ast.Name):
                    shadow.add(sub.optional_vars.id)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                shadow.add(sub.name)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                shadow.add(sub.name)
        return shadow

    @staticmethod
    def _for_target_name(node: ast.For | ast.AsyncFor) -> str | None:
        if isinstance(node.target, ast.Name):
            return node.target.id
        return None

    def _call_func_text(self, func: ast.expr) -> str | None:
        """调用对象 → 文本名(最右属性),用于分批上下文/关键字粗判。"""
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _resolve_call(self, call: ast.Call, shadow: set[str]) -> str | None:
        """调用点 → 限定名(app.core.sources.get_spot 式);解析不到返回 None。"""
        func = call.func
        if isinstance(func, ast.Name):
            return self._resolve_name(func.id, shadow)
        if isinstance(func, ast.Attribute):
            chain: list[str] = []
            cur: ast.expr = func
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                return None
            root = cur.id
            if root in shadow:
                return None
            mod = self.import_map.get(root)
            if mod is None:
                return None
            return f"{mod}.{'.'.join(reversed(chain))}"
        return None

    def _resolve_name(self, name: str, shadow: set[str]) -> str | None:
        if name in shadow:
            return None
        mod = self.import_map.get(name)
        if mod:
            return f"{mod}.{name}"
        if name in self.local_defs:
            return f"{self.qualname}.{name}"
        return None

    # ---- 模块级标记 ----
    def _scan_module_assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        # shared_session:模块级 requests.Session() 赋值
        if isinstance(node.value, ast.Call) and self._is_requests_session(
            node.value.func
        ):
            line = node.lineno
            if not any(
                m["kind"] == "shared_session" and m["line"] == line for m in self.marks
            ):
                self.marks.append(
                    {
                        "kind": "shared_session",
                        "file": self.rel,
                        "line": line,
                        "detail": "模块级 requests.Session() 赋值",
                    }
                )
        # global_write:模块级对可变容器的赋值——仅记候选,函数体内有修改才标记(见 analyze 尾)
        if self._is_container_literal(node.value):
            targets = list(self._assign_targets(node))
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            for n in names:
                self._global_containers.add(n)
                self._container_assigns.append(
                    {
                        "name": n,
                        "line": node.lineno,
                        "detail": ast.unparse(node.value)[:80],
                    }
                )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Subscript):
            # 模块级 CACHE["k"] = v 修改(模块级即运行时写,直接标记)
            self._mutated_globals.add(self._subscript_base(node.target))
            self.marks.append(
                {
                    "kind": "global_write",
                    "file": self.rel,
                    "line": node.lineno,
                    "detail": "模块级下标写入可变容器",
                }
            )

    @staticmethod
    def _subscript_base(node: ast.Subscript) -> str:
        """CACHE['k'] → 'CACHE'(逐层解包嵌套下标)"""
        while isinstance(node, ast.Subscript):
            node = node.value
        return node.id if isinstance(node, ast.Name) else ""

    def _scan_module_expr(self, node: ast.Expr) -> None:
        if not isinstance(node.value, ast.Call):
            return
        func = node.value.func
        # 模块级容器修改方法调用:REGISTRY.append(...)
        # 只计入 mutated(容器赋值候选经 analyze 尾统一标记,避免初始化注册双重报)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.attr in MUTABLE_METHODS:
                self._mutated_globals.add(func.value.id)
        # os_exit:全模块任意位置 os._exit(
        if self._is_os_exit(func):
            self.marks.append(
                {
                    "kind": "os_exit",
                    "file": self.rel,
                    "line": node.lineno,
                    "detail": "调用 os._exit(",
                }
            )

    @staticmethod
    def _is_requests_session(func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id == "requests" and func.attr == "Session"
        return False

    @staticmethod
    def _is_os_exit(func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id == "_exit"
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id == "os" and func.attr == "_exit"
        return False

    @staticmethod
    def _is_container_literal(value: ast.expr) -> bool:
        return isinstance(value, (ast.List, ast.Dict, ast.Set))

    @staticmethod
    def _string_literals(node: ast.AST) -> list[tuple[str, int]]:
        """提取节点中的字符串字面量(含 f-string 常量片段与 + 拼接)。"""
        out: list[tuple[str, int]] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.value, node.lineno))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{}")
            out.append(("".join(parts), node.lineno))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for side in (node.left, node.right):
                out.extend(ModuleAnalyzer._string_literals(side))
        return out


def _reverse_callers(funcs: dict[str, dict]) -> None:
    """根据 callees 反查 callers(限定名 → 节点 key 反查表)。

    语义:若 A 的 callees 含 B,则 B 的 callers 追加 A(调用者)。"""
    key_by_qual: dict[str, str] = {}
    for key, info in funcs.items():
        file, _, name = key.rpartition("::")
        qual = f"{module_qualname(file)}.{name}"
        key_by_qual[qual] = key
    callee_map: dict[str, list[str]] = {}  # 被调节点 key → [调用者 key]
    for key, info in funcs.items():
        for q in info.get("callees", []):
            ck = key_by_qual.get(q)
            if ck and ck != key and key not in callee_map.setdefault(ck, []):
                callee_map[ck].append(key)
    for ck, callers in callee_map.items():
        if ck in funcs:
            funcs[ck]["callers"] = callers
    for key, info in funcs.items():
        info.setdefault("callers", [])
        info["used_as_arg"] = key in info.get("_used_arg_keys", set())


def build(root: Path) -> dict:
    """构建调用图:root 为 app 包目录(如 backend/app),返回可序列化 dict。"""
    root = Path(root).resolve()
    files = walk_files(root)
    module_index = {
        module_qualname(rel_path(root, p)): rel_path(root, p) for p in files
    }
    resolver = ImportResolver(module_index)

    modules: dict[str, dict] = {}
    routes: list[dict] = []
    funcs: dict[str, dict] = {}
    marks: list[dict] = []

    for path in files:
        rel = rel_path(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            # 语法错误文件:记录占位,不中断整图
            modules[rel] = {
                "file": rel,
                "imports": [],
                "functions": [],
                "error": "parse failed",
            }
            continue
        analyzer = ModuleAnalyzer(rel, tree, resolver)
        analyzer.analyze()
        modules[rel] = {
            "file": rel,
            "imports": sorted(set(analyzer.imports)),
            "functions": sorted(analyzer.funcs.keys()),
        }
        funcs.update(analyzer.funcs)
        routes.extend(analyzer.routes)
        marks.extend(analyzer.marks)
        # used_as_arg 从 analyzer 收集回填到 funcs
        for k in analyzer.used_as_arg:
            if k in funcs:
                funcs[k].setdefault("_used_arg_keys", set()).add(k)

    # 路由 handler 的 callees 与函数节点合并:handler 即函数节点,直接引用
    for r in routes:
        key = f"{r['file']}::{r['handler']}"
        node = funcs.get(key)
        if node:
            r["callees"] = list(node.get("callees", []))

    _reverse_callers(funcs)

    stats = {
        "files": len(files),
        "routes": len(routes),
        "functions": len(funcs),
        "marks": len(marks),
    }
    return {
        "modules": modules,
        "routes": routes,
        "calls": {
            k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
            for k, v in funcs.items()
        },
        "marks": marks,
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后端调用图构建器(ast 静态分析)")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent.parent / "backend" / "app"),
        help="扫描根目录(默认 backend/app)",
    )
    parser.add_argument("--out", default=None, help="输出 JSON 文件路径(默认 stdout)")
    args = parser.parse_args(argv)

    graph = build(Path(args.root))
    text = json.dumps(graph, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        s = graph["stats"]
        print(
            f"图已写入 {args.out}: 文件 {s['files']} / 路由 {s['routes']} / 函数 {s['functions']} / 标记 {s['marks']}"
        )
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
