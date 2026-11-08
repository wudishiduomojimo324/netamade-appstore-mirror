#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从「软件官方上游」自动更新镜像仓库里的包（GitHub Actions runner 上执行）

原则:
  - 只处理 upstream.json 注册表里列出的应用；未列出的（C 类：美式/哪吒私有魔改包）
    一个字节都不碰 —— 包括 Release 资产、apps.json 条目、icons。
  - 上游包必须能直接安装：下载后校验 ZIP 头 (PK\\x03\\x04)。
  - 同名旧资产先删再传，避免 GitHub 自动加 (1) 后缀。
  - 更新 apps.json 时只改被更新应用的 version/latestFile/latestSize/latestUrl/files，
    其它字段（name/package/icon/description）与其它应用原样保留。

版本判定:
  - github_release: release tag / published_at 与仓库里记录的上游 tag 比对（存 upstream-state.json）
  - official_page : 从 APK 直链里解析版本号，与 state 比对
  state 不存在时视为首次运行 → 拉取并记录（不误删已有包）。
"""
import base64
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

MIRROR = os.environ.get("MIRROR_REPO", "wudishiduomojimo324/netamade-appstore-mirror")
TAG = os.environ.get("RELEASE_TAG", "appstore-202610")
TOKEN = os.environ.get("GH_TOKEN", "")
REPO_DIR = os.environ.get("GITHUB_WORKSPACE", ".")
WORK = "/tmp/official"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def api(path, repo=None, timeout=90):
    url = f"https://api.github.com/repos/{repo or MIRROR}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Cache-Control", "no-cache")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def api_raw(path, repo=None, timeout=90):
    url = f"https://api.github.com/repos/{repo or MIRROR}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github.raw")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def api_delete(path):
    url = f"https://api.github.com/repos/{MIRROR}/{path}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def api_put(path, content_str, message):
    """提交文本到仓库（自动带 sha）。"""
    try:
        existing = api(f"contents/{urllib.parse.quote(path)}")
        sha = existing.get("sha")
    except Exception:
        sha = None
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    url = f"https://api.github.com/repos/{MIRROR}/contents/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PUT")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def resolve_github_release(app):
    repo = app["repo"]
    rels = api(f"releases?per_page=5", repo=repo)
    if not rels:
        raise RuntimeError("仓库没有 release")
    rel = rels[0]
    tag = rel.get("tag_name") or ""
    pat = re.compile(app.get("assetPattern", r"\.apk$"), re.I)
    assets = [a for a in rel.get("assets", []) if pat.search(a["name"])]
    if not assets:
        raise RuntimeError(f"release {tag} 没有匹配 assetPattern 的 APK")
    a = assets[0]
    return {
        "upstreamVersion": tag,
        "publishedAt": rel.get("published_at", ""),
        "fileName": a["name"],
        "url": a["browser_download_url"],
        "size": a["size"],
    }


def resolve_official_page(app):
    html = http_get(app["page"], timeout=30)
    pat = re.compile(app["apkUrlPattern"])
    urls = pat.findall(html)
    if not urls:
        raise RuntimeError("下载页没解析出 APK 直链")
    url = urls[0].replace("&amp;", "&")
    ver = ""
    if app.get("versionFromUrl"):
        m = re.search(app["versionFromUrl"], url)
        if m:
            ver = m.group(1)
    fname = os.path.basename(urllib.parse.urlparse(url).path)
    return {
        "upstreamVersion": ver or fname,
        "publishedAt": "",
        "fileName": fname,
        "url": url,
        "size": 0,
    }


def main():
    os.makedirs(WORK, exist_ok=True)
    cfg = json.load(open(os.path.join(REPO_DIR, "upstream.json"), encoding="utf-8"))
    apps_cfg = cfg["apps"]
    print(f"注册表: {len(apps_cfg)} 个应用（其余应用保持原样）", flush=True)

    # 读状态（注意必须走 contents/ 路径，否则 404 会被吞成空状态导致每天重复下载）
    try:
        state = json.loads(api_raw("contents/upstream-state.json"))
    except Exception as e:
        print(f"状态文件读取失败（首次运行属正常）: {str(e)[:100]}", flush=True)
        state = {}
    state.setdefault("versions", {})
    print(f"已记录版本: {len(state['versions'])} 个", flush=True)

    # 现有 Release 资产
    rel = api(f"releases/tags/{urllib.parse.quote(TAG)}")
    assets = {a["name"]: a for a in rel.get("assets", [])}

    # apps.json（稍后更新）
    apps_json_text = api_raw("contents/apps.json")
    apps_json = json.loads(apps_json_text)
    by_code = {a["code"]: a for a in apps_json}

    updated, skipped, failed = [], [], []
    for app in apps_cfg:
        code = app["code"]
        name = app["name"]
        try:
            if app["type"] == "github_release":
                info = resolve_github_release(app)
            elif app["type"] == "official_page":
                info = resolve_official_page(app)
            else:
                raise RuntimeError(f"未知类型 {app['type']}")

            ver = info["upstreamVersion"]
            prev = state["versions"].get(code, {})
            print(f"[{code}] {name}: 上游 {ver} / 已记录 {prev.get('version','(无)')}", flush=True)

            if prev.get("version") == ver and prev.get("asset") in assets:
                skipped.append(code)
                print("      → 已是最新，跳过", flush=True)
                continue

            # 下载（runner 内网快）
            # 资产名沿用 dir__file 规范，但用官方文件名（含版本号）
            asset_name = f"{code}__{info['fileName']}"
            dst = os.path.join(WORK, asset_name)
            run(["curl", "-sL", "--retry", "5", "--retry-delay", "2",
                 "-o", dst, info["url"]], timeout=1200)
            size = os.path.getsize(dst)
            if size < 10240:
                raise RuntimeError(f"下载内容仅 {size} 字节")

            # 必须能直接安装：校验 ZIP 头
            with open(dst, "rb") as f:
                head = f.read(4)
            if head != b"PK\x03\x04":
                raise RuntimeError(f"非 ZIP 头 {head!r}（疑似加密/带锁包），拒绝入库")

            # 删该应用的旧资产（只删同 code 前缀，绝不碰别的）
            for an in list(assets.keys()):
                if an.startswith(code + "__") and an != asset_name:
                    try:
                        api_delete(f"releases/assets/{assets[an]['id']}")
                        del assets[an]
                        print(f"      删旧资产 {an}", flush=True)
                    except Exception as e:
                        print(f"      ! 删旧失败 {an}: {str(e)[:60]}", flush=True)

            # 上传
            run(["gh", "release", "upload", TAG, dst, "--repo", MIRROR, "--clobber"], timeout=1200)
            os.remove(dst)

            url = f"https://github.com/{MIRROR}/releases/download/{TAG}/{asset_name}"
            assets[asset_name] = {"size": size}

            # 只更新该应用条目，其它字段保留。
            # version 去掉开头的 v（GitHub tag 常带 v，展示统一不带），
            # package / versionCode / icon / description 等人工补全过的字段绝不覆盖。
            display_ver = ver[1:] if isinstance(ver, str) and ver.startswith("v") else ver
            if code in by_code:
                e = by_code[code]
                e["version"] = display_ver
                e["latestFile"] = info["fileName"]
                e["latestSize"] = size
                e["latestUrl"] = url
                e["files"] = [info["fileName"]]
                e["error"] = None
            else:
                apps_json.append({
                    "code": code, "name": name, "package": None,
                    "version": display_ver, "versionCode": None, "error": None,
                    "icon": None, "files": [info["fileName"]],
                    "latestFile": info["fileName"], "latestSize": size, "latestUrl": url,
                })

            state["versions"][code] = {"version": ver, "asset": asset_name, "size": size}
            updated.append((code, ver, size))
            print(f"      ✓ 更新为 {ver} ({size/1048576:.1f}MB)", flush=True)
        except Exception as e:
            failed.append((code, str(e)[:180]))
            print(f"      ✗ {name}: {str(e)[:180]}", flush=True)

    # 提交 apps.json + state
    if updated:
        new_apps = json.dumps(apps_json, ensure_ascii=False, indent=1)
        st = api_put("apps.json", new_apps, f"chore: 官方上游自动更新 {len(updated)} 个应用")
        print(f"apps.json 提交: HTTP {st}", flush=True)
    st = api_put("upstream-state.json", json.dumps(state, ensure_ascii=False, indent=1),
                 "chore: update upstream state")
    print(f"upstream-state.json 提交: HTTP {st}", flush=True)

    print(f"\n完成: 更新 {len(updated)}, 跳过 {len(skipped)}, 失败 {len(failed)}", flush=True)
    for c, e in failed:
        print(f"  FAILED {c}: {e}", flush=True)

    with open(os.path.join(WORK, "result.json"), "w") as f:
        json.dump({"updated": updated, "skipped": skipped, "failed": failed}, f)


if __name__ == "__main__":
    main()
