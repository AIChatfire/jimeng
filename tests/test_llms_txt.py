"""`/llms.txt` + 根路径落地页：说明书**不许漂移成假信息**。

三条硬约束（与 baidu-service 的同一套约定）：
1. 能力表 / 单价 / 上游模型选项 **从注册表 `catalog()` 派生**（对账，不靠人肉同步）；
2. 错误表**从 `app/errors.py` 的异常类派生**（防"文档里有、代码里没有"的幽灵码）；
3. 两个端点都**免鉴权**（LLM/Agent 要能直接读）。
"""

from __future__ import annotations

import inspect

from app import errors as E
from app.llms_txt import _error_rows, render
from app.models import catalog

def test_llms_txt_is_public_markdown(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    assert body.startswith("# jimeng-service"), body[:60]
    # 计费是最容易踩的坑，必须写在最显眼处
    assert "先读：计费" in body and "建任务即计费" in body
    # 两套形态都要交代
    assert "/async/v1/images/generations" in body and "/v1/images/generations" in body
    assert "/api/v3/contents/generations/tasks" in body


def test_llms_txt_derives_from_catalog(settings):
    txt = render(settings)
    items = catalog()
    for it in items:
        assert f"`{it['id']}`" in txt, f"{it['id']} 未出现在说明书"
    # 能力表行数 >= 注册表长度（防止漏抄；多出的是上游模型选项表）
    assert len([ln for ln in txt.splitlines() if ln.startswith("| `")]) >= len(items)
    for um in [m for it in items for m in (it.get("upstream_models") or [])]:
        assert f"`{um['key']}`" in txt, um["key"]


def test_llms_txt_error_table_matches_source(settings):
    """错误表必须与源码里的异常类一一对应（双向）。"""
    rows = _error_rows()
    src_codes = {
        (cls.err_code or name)
        for name, cls in vars(E).items()
        if inspect.isclass(cls) and issubclass(cls, E.AdapterError) and cls is not E.AdapterError
    }
    assert {r[0] for r in rows} == src_codes, "说明书的错误码与 app/errors.py 不一致"
    txt = render(settings)
    for code in src_codes:
        assert f"`{code}`" in txt, f"{code} 未出现在错误表"


def test_index_is_public_landing_page(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "<title>jimeng-service" in body
    for path in ("/llms.txt", "/v1/models", "/healthz"):
        assert f'href="{path}"' in body
    assert f"{len(catalog())} 项" in body, "页面计数必须来自注册表"
