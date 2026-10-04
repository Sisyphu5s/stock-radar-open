"""代码工具：交易所板块判定 / 股票代码规范化 / 批量切分（纯函数，无 I/O）。

由原 common.py 迁移而来（common-split），函数体逐字搬移，行为不改。
"""


def sector_of(code: str) -> str:
    """交易所板块（按代码前缀可靠推导，任何数据源可用）。

    口径说明：002 前缀为深圳中小板，2021-04 中小板已并入深主板，
    故统一判定为「深主板」（与 signals.py 原口径一致，修正 market.py 的「中小板」旧口径）。
    """
    c = str(code).split(".")[0]
    if c.startswith("688"):
        return "科创板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if c.startswith(("000", "001", "003")):
        return "深主板"
    if c.startswith("002"):
        return "深主板"
    if c.startswith(("4", "8", "920")):
        return "北交所"
    return "其他"


def normalize_code(code: str) -> str:
    """统一为 600519.SH / 000001.SZ / 8xxxxx.BJ / 920xxx.BJ 格式；已带后缀原样返回。"""
    c = str(code).strip()
    if "." in c:
        return c
    if c.startswith("920"):
        return f"{c}.BJ"
    if c.startswith(("6", "9")):
        return f"{c}.SH"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return f"{c}.SZ"


def bare_code(code: str) -> str:
    """去交易所后缀：'600519.SH' → '600519'。"""
    return str(code).split(".")[0]


def in_chunks(values, batch: int = 400):
    """大集合按批切分（生成器，逐批 yield list），供 SQLite IN 子句分批查询。

    SQLite 变量上限 999，批量 IN 按 400/批规避 too many SQL variables。
    保持传入顺序切分、不排序（排序语义由调用方决定）；batch <= 0 视为一批全给。
    """
    if batch <= 0:
        yield list(values)
        return
    for i in range(0, len(values), batch):
        yield values[i : i + batch]
