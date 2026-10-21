# -*- coding: utf-8 -*-
"""出站体积闸门套件（上游 ~30MB body 上限 / WinError 10053 那一类）。

    python tests/_test_payload_budget.py

钉住的五条不变量：
  1) 不超线的请求**一个字节都不动**（返回 False，body 与原来逐字节相同）；
  2) 超线时先丢**最老的**图片，保留最近 QD_PAYLOAD_KEEP_IMAGES 张；
  3) 丢图不够时再压最大的文本块（工具结果），而不是把整条消息删掉；
  4) 实在压不下去（例如只有一张巨图）-> 抛 413，且 detail 带
     PAYLOAD_TOO_LARGE_PREFIX，供 friendly_upstream_error 翻译成中文文案；
  5) 压完之后 encoded 长度必须 <= cap（按 base64 变体的 4*ceil(n/3) 算）。
"""
import importlib.util
import io
import json
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))

_spec = importlib.util.spec_from_file_location(
    "qoder_proxy_budget_under_test", os.path.join(ROOT, "qoder_proxy.py"))
P = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = P
_spec.loader.exec_module(P)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def data_url(mb):
    """一个 shape 合法的 data URL（内容不重要，只看体积）。"""
    return "data:image/png;base64," + "A" * int(mb * 1024 * 1024)


def body_with_images(sizes_mb, tail_text_mb=0):
    msgs = []
    for i, mb in enumerate(sizes_mb):
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": "图 %d" % i},
            {"type": "image_url", "image_url": {"url": data_url(mb)}}]})
        msgs.append({"role": "assistant", "content": "ok"})
    if tail_text_mb:
        msgs.append({"role": "tool", "content": "L" * int(tail_text_mb * 1048576)})
    return {"messages": msgs, "chat_context": {"text": {"text": "hi"}}, "tools": []}


CAP_MB = P.PAYLOAD_CAP_BYTES / 1048576.0
print("cap=%.0fMB keep_images=%d steps=%s"
      % (CAP_MB, P.PAYLOAD_KEEP_IMAGES, P.PAYLOAD_TEXT_KEEP_STEPS))


# ---- 1) 小请求逐字节不变 ----
small = {"messages": [{"role": "user", "content": "hello"}], "tools": []}
snapshot = json.dumps(small, ensure_ascii=False)
touched = P.enforce_payload_budget(small, model="m")
check("小请求不触发闸门", touched is False)
check("小请求逐字节不变", json.dumps(small, ensure_ascii=False) == snapshot)


# ---- 2) 一堆积图：丢最老的，保住最新的 ----
per_img = 2.0
count = int(CAP_MB / per_img) + 4          # 一定超线
body = body_with_images([per_img] * count)
before = P._encoded_len(body)
last_url = body["messages"][-2]["content"][1]["image_url"]["url"]
changed = P.enforce_payload_budget(body, model="m")
after = P._encoded_len(body)
kept = len(list(P._iter_image_parts(body)))
check("超线请求被处理", changed is True)
check("encoded 压到上限内", after <= P.PAYLOAD_CAP_BYTES,
      "%.1fMB > %.0fMB" % (after / 1048576.0, CAP_MB))
check("确实变小了", after < before)
check("保留的图片数量 == keep_images 及以上", kept >= max(1, P.PAYLOAD_KEEP_IMAGES),
      "kept=%d" % kept)
check("最新一张图原封不动",
      body["messages"][-2]["content"][1]["image_url"]["url"] == last_url)
check("被丢的图片换成可读占位文本",
      any(isinstance(p, dict) and p.get("type") == "text"
          and "image omitted" in (p.get("text") or "")
          for p in body["messages"][0]["content"]))


# ---- 3) 图不够丢时压文本块，不删消息 ----
text_body = {"messages": [{"role": "tool", "content": "L" * int((CAP_MB + 8) * 1048576)}]}
n_before = len(text_body["messages"])
P.enforce_payload_budget(text_body, model="m")
check("纯文本超线也能压到线下",
      P._encoded_len(text_body) <= P.PAYLOAD_CAP_BYTES,
      "%.1fMB" % (P._encoded_len(text_body) / 1048576.0))
check("压文本不删消息", len(text_body["messages"]) == n_before)
check("文本块保留了首尾（长度合理）",
      0 < len(text_body["messages"][0]["content"]) < 200000)


# ---- 4) 压不下去 -> 413 + 前缀 ----
single = {"messages": [{"role": "user", "content": [
    {"type": "text", "text": "hi"},
    {"type": "image_url", "image_url": {"url": data_url(CAP_MB * 1.6)}}]}]}
raised = None
try:
    P.enforce_payload_budget(single, model="m")
except urllib.error.HTTPError as exc:
    raised = exc
check("单张巨图丢不掉时抛 HTTPError", raised is not None)
if raised is not None:
    detail = raised.read().decode("utf-8")
    check("状态码 413", raised.code == 413, str(raised.code))
    check("detail 带 payload-too-large 前缀",
          detail.startswith(P.PAYLOAD_TOO_LARGE_PREFIX), detail[:60])
    msg, etype = P.friendly_upstream_error(413, detail)
    check("客户端文案是中文且指明超限", "超过出站上限" in msg, msg[:60])
    check("err_type = payload_too_large", etype == "payload_too_large", etype)
    check("文案里给出实际体积", "MB" in msg, msg[:120])


# ---- 5) 关闭闸门（cap=0）时永远不动 ----
old_cap = P.PAYLOAD_CAP_BYTES
try:
    P.PAYLOAD_CAP_BYTES = 0
    body = body_with_images([3.0] * 12)
    snap = json.dumps(body, ensure_ascii=False)
    check("cap=0 时短路不处理",
          P.enforce_payload_budget(body, model="m") is False)
    check("cap=0 时逐字节不变", json.dumps(body, ensure_ascii=False) == snap)
finally:
    P.PAYLOAD_CAP_BYTES = old_cap


print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed, 0 skipped"
      % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
if FAIL:
    sys.exit(1)
