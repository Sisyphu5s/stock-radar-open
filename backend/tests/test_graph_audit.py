"""图审计器测试:临时目录最小假项目,直接调 graph_build.build 与 graph_audit.run_bX。

覆盖:
- 图构建:modules/imports/routes/callees/marks 结构与统计
- B1:跨层 SCC 环 + api→storage 直穿(可白名单豁免)
- B2:死函数 WARN
- B3:热点路径命中 SessionLocal(≤4 跳 BFS)
- B4:raw_in 铁律标注 FAIL(可白名单豁免)
- schema_dump.flatten_schema 纯函数($ref 递归/array/anyOf null)
- schema_dump.dump_base_schemas 纯函数(基库形状快照 + contract_kind)
不联网、不 import app.main。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

# scripts/backend 目录插入 sys.path,便于 import graph_build/graph_audit
SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts" / "backend"
sys.path.insert(0, str(SCRIPTS))

import graph_audit  # noqa: E402
from graph_build import build  # noqa: E402
from schema_dump import dump_base_schemas, flatten_schema, is_nullable  # noqa: E402

# ---- 假项目:7 个文件,含跨层环/直穿/死函数/raw_in/sessionlocal ----
FAKE_FILES = {
    "fakeapp/__init__.py": "",
    "fakeapp/api/__init__.py": "",
    "fakeapp/api/routes.py": (
        "from fastapi import APIRouter\n"
        "from ..storage.db import SessionLocal\n"
        "from ..lib.helper import helper_used\n"
        "\n"
        "router = APIRouter(prefix='/market')\n"
        "\n"
        "@router.get('/snapshot')\n"
        "def snapshot():\n"
        "    db = SessionLocal()\n"
        "    helper_used()\n"
        "    q = f\"SELECT * FROM t WHERE code IN ({','.join(['1'])})\"\n"
        "    return {'ok': True}\n"
    ),
    "fakeapp/storage/__init__.py": "",
    "fakeapp/storage/db.py": (
        "from sqlalchemy.orm import sessionmaker\n"
        "\n"
        "SessionLocal = sessionmaker()\n"
        "\n"
        "def a():\n"
        "    return b()\n"
        "\n"
        "def b():\n"
        "    return a()\n"
    ),
    "fakeapp/lib/__init__.py": "",
    "fakeapp/lib/helper.py": (
        "from ..api.routes import router  # 制造 lib→api 环\n"
        "\n"
        "def helper_used():\n"
        "    return 1\n"
        "\n"
        "def dead():\n"
        "    return 2\n"
        "\n"
        "class Hooks:\n"
        "    # 类内私有方法与 pydantic model_* 钩子:框架反射调用,B2 应豁免\n"
        "    def _private(self):\n"
        "        return 1\n"
        "\n"
        "    def model_post_init(self, __context):\n"
        "        return 2\n"
    ),
}


def make_fake_project(tmp_path: Path) -> Path:
    """写入假项目文件,返回 fakeapp 包目录(作为 build 的 root,rel 不带前缀)。"""
    base = tmp_path / "proj"
    for rel, content in FAKE_FILES.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return base / "fakeapp"


def build_fake(tmp_path: Path) -> tuple[dict, Path]:
    root = make_fake_project(tmp_path)
    return build(root), root


# ---- 图构建 ----
def test_build_structure(tmp_path):
    graph, _ = build_fake(tmp_path)
    stats = graph["stats"]
    assert stats["files"] == 7
    assert stats["routes"] == 1
    # modules:imports 已解析到文件路径
    mod_routes = graph["modules"]["api/routes.py"]
    assert "storage/db.py" in mod_routes["imports"]
    assert "lib/helper.py" in mod_routes["imports"]
    # routes 结构与 callees 解析
    route = graph["routes"][0]
    assert route["path"] == "/market/snapshot"
    assert route["method"] == "GET"
    assert route["handler"] == "snapshot"
    assert "storage.db.SessionLocal" in route["callees"]
    assert "lib.helper.helper_used" in route["callees"]


def test_build_module_internal_calls(tmp_path):
    graph, _ = build_fake(tmp_path)
    a = graph["calls"]["storage/db.py::a"]
    b = graph["calls"]["storage/db.py::b"]
    # 模块内精确解析:a ↔ b 互调
    assert "storage.db.b" in a["callees"]
    assert "storage.db.a" in b["callees"]
    # 反向 callers
    assert "storage/db.py::a" in b["callers"]
    assert "storage/db.py::b" in a["callers"]


def test_build_marks(tmp_path):
    graph, _ = build_fake(tmp_path)
    kinds = {m["kind"] for m in graph["marks"]}
    assert "sessionlocal" in kinds
    assert "raw_in" in kinds
    sl = [m for m in graph["marks"] if m["kind"] == "sessionlocal"]
    assert sl[0]["file"] == "api/routes.py"
    assert sl[0]["callee"] == "storage.db.SessionLocal"


# ---- B1 分层环 ----
def test_b1_cross_layer_scc_and_pierce(tmp_path):
    graph, _ = build_fake(tmp_path)
    issues = graph_audit.run_b1(graph)
    keys = {i["key"] for i in issues}
    # 跨层环:api/routes.py ↔ lib/helper.py
    assert any("api/routes.py" in k and "lib/helper.py" in k for k in keys)
    # api → storage 直穿
    assert "api/routes.py->storage/db.py" in keys
    for i in issues:
        assert i["level"] == "FAIL"


def test_b1_whitelist_exempts(tmp_path):
    graph, _ = build_fake(tmp_path)
    wl = {
        "b1_edges": [
            "api/routes.py->storage/db.py",
            "api/routes.py->lib/helper.py",
            "lib/helper.py->api/routes.py",
            "<->".join(sorted(["api/routes.py", "lib/helper.py"])),
        ],
        "b2_dead": [],
        "b3_hot": [],
        "b4_marks": [],
    }
    issues = graph_audit.run_b1(graph, wl)
    assert issues == []


# ---- B2 死代码 ----
def test_b2_dead_warn(tmp_path):
    graph, root = build_fake(tmp_path)
    issues = graph_audit.run_b2(graph, None, root)
    keys = {i["key"] for i in issues}
    assert "lib/helper.py::dead" in keys
    # helper_used 有调用者,不算死代码
    assert "lib/helper.py::helper_used" not in keys
    # 路由 handler 豁免
    assert "api/routes.py::snapshot" not in keys
    # 类内私有方法与 pydantic model_* 钩子:框架反射调用,豁免
    assert "lib/helper.py::Hooks._private" not in keys
    assert "lib/helper.py::Hooks.model_post_init" not in keys
    for i in issues:
        assert i["level"] == "WARN"


# ---- B3 热路径 ----
def test_b3_hot_path_sessionlocal(tmp_path):
    graph, _ = build_fake(tmp_path)
    issues = graph_audit.run_b3(graph)
    assert len(issues) == 1
    i = issues[0]
    assert i["level"] == "FAIL"
    assert i["key"] == "/market/snapshot"
    assert "storage.db.SessionLocal" in i["evidence"]


def test_b3_whitelist_exempts(tmp_path):
    graph, _ = build_fake(tmp_path)
    wl = {"b1_edges": [], "b2_dead": [], "b3_hot": ["/market/snapshot"], "b4_marks": []}
    assert graph_audit.run_b3(graph, wl) == []


# ---- B4 铁律标注 ----
def test_b4_raw_in_fail(tmp_path):
    graph, _ = build_fake(tmp_path)
    issues = graph_audit.run_b4(graph)
    raw = [i for i in issues if "raw_in" in i["evidence"]]
    assert len(raw) == 1
    assert raw[0]["level"] == "FAIL"
    assert raw[0]["key"] == "api/routes.py:11"


def test_b4_whitelist_exempts(tmp_path):
    graph, _ = build_fake(tmp_path)
    wl = {"b1_edges": [], "b2_dead": [], "b3_hot": [], "b4_marks": ["api/routes.py:11"]}
    assert graph_audit.run_b4(graph, wl) == []


# ---- schema_dump 纯函数 ----
def test_flatten_schema_ref_and_array():
    schemas = {
        "Item": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "price": {"type": "number", "nullable": True},
            },
        },
        "Resp": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/Item"},
                },
                "tag": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        },
    }
    fields = flatten_schema({"$ref": "#/components/schemas/Resp"}, schemas)
    assert fields["count"] == {"type": "number", "nullable": False}
    assert fields["items"] == {"type": "array(object)", "nullable": False}
    # 数组元素对象展开为 field[].xxx
    assert fields["items[].code"] == {"type": "string", "nullable": False}
    assert fields["items[].price"] == {"type": "number", "nullable": True}
    # anyOf 含 null → nullable
    assert fields["tag"]["nullable"] is True
    assert fields["tag"]["type"] == "string"


def test_is_nullable_helpers():
    schemas = {}
    assert is_nullable({"type": ["string", "null"]}, schemas) is True
    assert is_nullable({"type": "string"}, schemas) is False
    assert is_nullable({"nullable": True, "type": "string"}, schemas) is True


# ---- schema_dump 基库形状快照 ----
def test_dump_base_schemas():
    class FakeResp(BaseModel):
        contract_kind: ClassVar[str] = "envelope"
        data: int

    class FakePage(BaseModel):
        total: int
        items: list[str]

    FakeResp.__module__ = "fake_schemas"
    FakePage.__module__ = "fake_schemas"
    fake_module = type("fake_schemas", (), {"FakeResp": FakeResp, "FakePage": FakePage})()
    fake_module.__name__ = "fake_schemas"
    out = dump_base_schemas(fake_module)
    assert set(out) == {"FakeResp", "FakePage"}
    assert out["FakeResp"]["kind"] == "envelope"
    assert out["FakePage"]["kind"] == "model"
    assert out["FakeResp"]["fields"]["data"] == {"type": "number", "nullable": False}
    assert out["FakePage"]["fields"]["items"] == {
        "type": "array(string)",
        "nullable": False,
    }
    assert out["FakePage"]["fields"]["total"] == {"type": "number", "nullable": False}


def test_dump_base_schemas_none_and_external():
    assert dump_base_schemas(None) == {}
    # 外部导入的类(__module__ 不匹配)与 BaseModel 自身应被排除
    fake_module = type(
        "fake_schemas",
        (),
        {"_external": BaseModel, "BaseModel": BaseModel, "not_a_model": 42},
    )()
    fake_module.__name__ = "fake_schemas"
    assert dump_base_schemas(fake_module) == {}


# ---- 编排冒烟:build + 全规则 + 白名单差分 ----
def test_full_pipeline(tmp_path):
    graph, root = build_fake(tmp_path)
    wl = graph_audit.load_whitelist(None)  # 缺省空白名单
    for rule_fn in (
        lambda g, w: graph_audit.run_b1(g, w),
        lambda g, w: graph_audit.run_b2(g, w, root),
        lambda g, w: graph_audit.run_b3(g, w),
        lambda g, w: graph_audit.run_b4(g, w),
    ):
        issues = rule_fn(graph, wl)
        assert isinstance(issues, list)
    # 全量白名单豁免后,FAIL 类规则应清空
    all_keys = {
        "b1_edges": [i["key"] for i in graph_audit.run_b1(graph)],
        "b2_dead": [i["key"] for i in graph_audit.run_b2(graph, None, root)],
        "b3_hot": [i["key"] for i in graph_audit.run_b3(graph)],
        "b4_marks": [i["key"] for i in graph_audit.run_b4(graph)],
    }
    wl_full = dict(all_keys)
    assert graph_audit.run_b1(graph, wl_full) == []
    assert graph_audit.run_b3(graph, wl_full) == []
    assert graph_audit.run_b4(graph, wl_full) == []
    # 图可 JSON 序列化(编排输出契约)
    json.dumps(graph, ensure_ascii=False)
