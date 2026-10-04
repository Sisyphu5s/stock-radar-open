"""OpenAPI 契约扁平化:import app.main 取 openapi(),把每路径×方法的 200 响应 schema 递归展开为扁平字段树。

用法:
  PYTHONPATH=backend .venv/bin/python scripts/backend/schema_dump.py [--out scripts/backend/contract.json]

输出 JSON:
  { "/market/snapshot": { "GET": { "fields": { "data.code": {"type": "string", "nullable": true} } } },
    "_schemas": { "Paginated": { "fields": {...}, "kind": "pagination" } },
    "_meta": { "paths_total": <全部 API 操作数>, "paths_with_schema": <有 JSON schema 的操作数>, "schema_defs": 3 } }

- 逐路径字段:来自路由 response_model(经 FastAPI openapi);无 response_model 的路由不出现在逐路径段。
- _schemas:API 层响应模型基库(app.api.schemas)的形状快照,即使路由尚未挂 response_model
  也产出响应契约段(P2-29 阶段一),kind 取自各模型 contract_kind。
- 字段 type 映射:integer/number→number, string→string, boolean→boolean,
  array→array(<item_type>), object→object;nullable 来自 anyOf/type 数组含 null 或 Optional。
- import app.main 仅创建 FastAPI 实例(create_app 模式),不启动服务;import 失败打印错误并退出码 1。
- 可 import 复用:flatten_schema(schema, schemas, prefix="") / dump_base_schemas(module)(纯函数,不依赖 app.main)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel

# ---- 类型映射(单一事实源)----
_NUMERIC = {"integer", "number"}


def map_type(raw: str | None, schemas: dict) -> str:
    """OpenAPI type → 输出 type;anyOf/oneOf 取第一个非 null 分支。"""
    if raw in _NUMERIC:
        return "number"
    if raw in ("string", "boolean", "object", "array", "null"):
        return raw
    if raw is None:
        return "object"
    return str(raw)


def _resolve(schema: dict, schemas: dict) -> dict:
    """解 $ref 引用(链式);无引用原样返回。"""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 8:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = schemas.get(name, {})
        seen += 1
    return schema


def _unwrap_union(schema: dict, schemas: dict) -> dict:
    """anyOf/oneOf 且无 type:取第一个非 null 分支展开。"""
    s = _resolve(schema, schemas)
    if s.get("type") is None and not s.get("properties"):
        for key in ("anyOf", "oneOf"):
            for sub in s.get(key, []) or []:
                sub_r = _resolve(sub, schemas)
                if sub_r.get("type") != "null":
                    merged = dict(sub_r)
                    merged["_nullable"] = True
                    return merged
    return s


def is_nullable(schema: dict, schemas: dict) -> bool:
    """nullable 判定:显式 nullable / type 数组含 null / anyOf·oneOf 含 null 分支。"""
    s = _resolve(schema, schemas)
    if isinstance(s.get("type"), list):
        return "null" in s["type"]
    if s.get("nullable") is True:
        return True
    for key in ("anyOf", "oneOf"):
        for sub in s.get(key, []) or []:
            if _resolve(sub, schemas).get("type") == "null":
                return True
    return bool(s.get("_nullable"))


def flatten_schema(schema: dict, schemas: dict, prefix: str = "") -> dict:
    """递归展开 schema 为扁平字段树 {field: {type, nullable}}。

    - object:字段展开为 prefix.field,递归子对象
    - array:自身记 array(<item_type>),元素为 object 时子字段展开为 field[].xxx
    - 嵌套字段同步记裸名(prefix 非空时):与前端 build.mjs collectReturnFields
      (depth>=1 时同时记 parent.child 与裸 child)对齐——R1 对前端消费字段做
      拆点匹配(cf.has(f) || cf.has(parts[last]) || parts.some(cf.has(p))),
      契约若只记嵌套名会漏掉前端从解包信封(data.data)处收集的裸字段名。
    纯函数:不依赖 app.main,可直接单测。
    """
    s = _unwrap_union(schema, schemas)
    if not s:
        return {}
    out: dict = {}
    if s.get("properties"):
        for name, prop in s["properties"].items():
            key = f"{prefix}.{name}" if prefix else name
            p = _resolve(prop, schemas)
            p = _unwrap_union(p, schemas)
            t = map_type(p.get("type"), schemas)
            nullable = is_nullable(prop, schemas)
            if t == "array":
                items = _resolve(p.get("items") or {}, schemas)
                items = _unwrap_union(items, schemas)
                item_type = map_type(items.get("type"), schemas)
                out[key] = {"type": f"array({item_type})", "nullable": nullable}
                if items.get("properties"):
                    out.update(flatten_schema(items, schemas, prefix=f"{key}[]"))
            elif t == "object":
                out[key] = {"type": "object", "nullable": nullable}
                if p.get("properties"):
                    out.update(flatten_schema(p, schemas, prefix=key))
            else:
                out[key] = {"type": t, "nullable": nullable}
            if prefix:
                out.setdefault(name, dict(out[key]))
    elif prefix:
        # 无 properties 但处于数组元素/嵌套位置的兜底:标记对象本身
        t = map_type(s.get("type"), schemas)
        out[prefix] = {"type": t, "nullable": is_nullable(schema, schemas)}
    return out


def dump_base_schemas(schema_module) -> dict:
    """基库响应模型形状快照:{Name: {fields, kind}}(纯函数,不依赖 app.main)。

    遍历模块内的 BaseModel 子类(排除导入与基类自身),model_json_schema() 扁平化;
    泛型基类未参数化时 pydantic 以自由 TypeVar 产出形状。kind 读类上 contract_kind。
    """
    if schema_module is None:
        return {}
    out: dict = {}
    for name in dir(schema_module):
        obj = getattr(schema_module, name)
        if not isinstance(obj, type) or obj is BaseModel or not issubclass(obj, BaseModel):
            continue
        if getattr(obj, "__module__", None) != schema_module.__name__:
            continue
        try:
            schema = obj.model_json_schema()
        except Exception:
            continue  # 形状无法生成(如特殊泛型参数)跳过,不阻断整体契约
        fields = flatten_schema(schema, schema.get("$defs", {}))
        out[name] = {
            "fields": fields,
            "kind": getattr(obj, "contract_kind", "model"),
        }
    return out


def _norm_path(path: str, api_prefix: str | None = None) -> str:
    """契约 path 归一化:对齐前端 URL 字面量(见 build.mjs 收集规则)。

    - 去掉 api 前缀(前端 api 调用点 URL 字面量不含 /api/v1);
    - 路径参数 {code} → :p(前端模板字符串插值统一记作 :p)。
    归一后 R1 的 urlMatch 前缀/相等匹配即可命中对应端点。
    """
    if api_prefix and path.startswith(api_prefix):
        path = path[len(api_prefix) :]
    return re.sub(r"\{[^}]+\}", ":p", path)


def dump_contract(app, schema_module=None, api_prefix: str | None = None) -> dict:
    """从 FastAPI app 的 openapi() 提取 200 响应字段树 + 基库形状快照。

    api_prefix 缺省由 app 所在包配置推导(避免调用方手传)。"""
    if api_prefix is None:
        try:
            from app.config import settings

            api_prefix = settings.api_prefix
        except Exception:
            api_prefix = None
    openapi = app.openapi()
    schemas = openapi.get("components", {}).get("schemas", {})
    paths = openapi.get("paths", {})
    contract: dict = {}
    paths_total = 0
    paths_with_schema = 0
    for raw_path, methods in paths.items():
        entry: dict = {}
        for method, op in methods.items():
            if method.lower() not in (
                "get",
                "post",
                "put",
                "delete",
                "patch",
                "options",
                "head",
            ):
                continue
            paths_total += 1
            responses = op.get("responses", {})
            resp = responses.get("200") or responses.get("2XX") or {}
            content = resp.get("content", {})
            body = content.get("application/json")
            if not body or "schema" not in body:
                continue
            fields = flatten_schema(body["schema"], schemas)
            if fields:
                entry[method.upper()] = {"fields": fields}
                paths_with_schema += 1
        if entry:
            contract[_norm_path(raw_path, api_prefix)] = entry
    contract["_schemas"] = dump_base_schemas(schema_module)
    # meta:前端 R1 依赖——paths_with_schema=0 说明逐路径 JSON 契约不可核；
    # schema_defs 为 API 响应模型基库的形状数。
    contract["_meta"] = {
        "paths_total": paths_total,
        "paths_with_schema": paths_with_schema,
        "schema_defs": len(contract["_schemas"]),
    }
    return contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAPI 契约扁平化导出")
    parser.add_argument("--out", default=None, help="输出 JSON 路径(默认 stdout)")
    args = parser.parse_args(argv)

    backend_dir = Path(__file__).resolve().parent.parent.parent / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    # 静音应用日志(import app.main 会触发 stockradar.* logger 初始化与 native 加载日志,
    # 契约 JSON 走 stdout,日志噪声会污染输出)
    import logging

    logging.disable(logging.CRITICAL)
    try:
        from app.main import app
    except Exception as exc:  # 依赖缺失/导入失败:打印错误退出码 1
        print(
            f"[ERROR] import app.main 失败(依赖缺失或导入错误): {exc}", file=sys.stderr
        )
        return 1
    finally:
        logging.disable(logging.NOTSET)

    # 基库形状:import 失败不阻断逐路径契约 dump(缺 _schemas 段,meta 反映 schema_defs=0)
    try:
        from app.api import schemas as api_schemas
    except Exception:
        api_schemas = None

    contract = dump_contract(app, api_schemas)
    text = json.dumps(contract, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        total = sum(
            len(m["fields"])
            for p, v in contract.items()
            if not p.startswith("_")
            for m in v.values()
        )
        meta = contract.get("_meta", {})
        print(
            f"契约已写入 {args.out}: {len(contract) - 2} 个路径 / {total} 个字段 "
            f"(meta: {meta.get('paths_total', 0)} 端点 / {meta.get('paths_with_schema', 0)} 有 schema "
            f"/ {meta.get('schema_defs', 0)} 基库形状)"
        )
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
