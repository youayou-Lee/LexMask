"""补查并发化试点 A/B 实测（窗口=1 vs 窗口=4）。

用法：python3 measure_supplement_ab.py <卷.pdf路径> <轮次标签>
流程：注册临时账号 → 登录 → 上传 → recognition-task（全类型）→
     轮询到 awaiting_review/failed → 抓 wall-clock 与 backend 日志里的
     re-query 计数 → 输出一行结论 JSON。
"""

import json
import os
import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:18904"
API = f"{BASE}/api/v1"
CONTAINER = "lexmask-pilot-backend-1"


def container_log_lines() -> int:
    out = subprocess.run(
        ["docker", "logs", CONTAINER], capture_output=True, text=True
    )
    return len((out.stdout + out.stderr).splitlines())


def log_ner_events(tag: str, since_line: int) -> dict:
    out = subprocess.run(
        ["docker", "logs", CONTAINER], capture_output=True, text=True
    )
    lines = (out.stdout + out.stderr).splitlines()[since_line:]
    plans = finished = 0
    windows = set()
    jobs_total = 0
    for line in lines:
        if "re-query plan" in line:
            plans += 1
        if "re-query finished" in line:
            finished += 1
            for tok in line.split():
                if tok.startswith("window="):
                    windows.add(tok.split("=", 1)[1])
                if tok.startswith("jobs="):
                    jobs_total += int(tok.split("=", 1)[1])
    return {"tag": tag, "requery_plans": plans, "requery_finished": finished,
            "windows": sorted(windows), "requery_jobs_total": jobs_total}


def main() -> int:
    path, tag = sys.argv[1], sys.argv[2]
    user, password = "pilot-baseline-window1-1000", "pilot-pass-123"  # 首管理员，跨轮共用
    c = httpx.Client(timeout=600.0, trust_env=False)
    r = c.post(f"{API}/auth/setup", json={"username": user, "password": password})
    if r.status_code not in (200, 201, 400):  # 400=已有管理员（重跑复用）
        print(json.dumps({"error": f"setup HTTP {r.status_code}: {r.text[:150]}"}))
        return 2
    r = c.post(f"{API}/auth/login", json={"username": user, "password": password})
    if r.status_code != 200:
        print(json.dumps({"error": f"login HTTP {r.status_code}: {r.text[:150]}"}))
        return 2
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = c.post(f"{API}/files/upload", headers=h,
               files={"file": (os.path.basename(path), open(path, "rb").read())})
    if r.status_code != 200:
        print(json.dumps({"error": f"upload HTTP {r.status_code}: {r.text[:150]}"}))
        return 2
    file_id = r.json()["file_id"] if "file_id" in r.json() else r.json()["id"]

    r = c.get(f"{API}/custom-types", headers=h, params={"enabled_only": "true"})
    if r.status_code != 200:
        print(json.dumps({"error": f"custom-types HTTP {r.status_code}"}))
        return 2
    ids = [t["id"] for t in r.json().get("custom_types", []) if t.get("id")]
    body = {"entity_type_ids": ids, "ocr_has_types": ids,
            "visual_feature_types": []}  # 试点栈无印章/签名服务，关视觉特征段
    r = c.post(f"{API}/files/{file_id}/recognition-task", headers=h, json=body)
    if r.status_code != 200:
        print(json.dumps({"error": f"task HTTP {r.status_code}: {r.text[:150]}"}))
        return 2
    ref = r.json()
    log_mark = container_log_lines()
    started = time.monotonic()
    status = None
    item = {}
    while time.monotonic() - started < 1800:
        time.sleep(5)
        r = c.get(f"{API}/jobs/{ref['job_id']}", headers=h)
        if r.status_code != 200:
            continue
        items = [it for it in (r.json().get("items") or [])
                 if it.get("item_id") == ref.get("item_id")] or (r.json().get("items") or [])
        item = items[0] if items else {}
        status = item.get("status")
        if status in ("awaiting_review", "completed", "failed"):
            break
    elapsed = round(time.monotonic() - started, 1)
    snap = c.get(f"{API}/files/{file_id}", headers=h).json()
    entities = snap.get("entities") or []
    result = {
        "tag": tag,
        "status": status,
        "elapsed_sec": elapsed,
        "entities": len(entities),
        **log_ner_events(tag, log_mark),
    }
    if status == "failed":
        result["error_message"] = str(item.get("error_message"))[:200]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if status in ("awaiting_review", "completed") else 1


if __name__ == "__main__":
    sys.exit(main())
