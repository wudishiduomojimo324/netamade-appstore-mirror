#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions runner 上生成 apps.json
- 读取仓库根 mirror-manifest.json
- 获取 release assets 列表
- 按目录取最大 APK，下载并解析元数据
- 生成 apps.json 到当前目录
"""
import json, os, subprocess, sys, urllib.request, urllib.parse

REPO = os.environ["REPO"]
TAG = os.environ["TAG"]
TOKEN = os.environ.get("GH_TOKEN", "")

def gh_api(path, accept=None):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", accept or "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    items = json.load(open("/tmp/appsinfo/manifest.json", encoding="utf-8"))
    assets = gh_api(f"releases/tags/{urllib.parse.quote(TAG)}")["assets"]
    asset_id = {a["name"]: a["id"] for a in assets}

    # 读取仓库 icons
    icons = []
    try:
        for entry in gh_api("contents/icons"):
            icons.append(entry["name"])
    except Exception as e:
        print("icons error:", e, file=sys.stderr)

    # 按 dir 分组，每组取最大文件
    by_dir = {}
    for x in items:
        by_dir.setdefault(x["dir"], []).append(x)

    from pyaxmlparser import APK

    apps = []
    for d in sorted(by_dir.keys()):
        files = by_dir[d]
        latest = max(files, key=lambda x: x["size"])
        asset_name = f"{d}__{latest['file']}"
        url = f"https://github.com/{REPO}/releases/download/{TAG}/{asset_name}"
        print(f"解析 {d} ({latest['file']}, {latest['size']/1048576:.1f}MB) ...", flush=True)
        meta = {}
        apk_path = None
        try:
            apk_path = f"/tmp/appsinfo/{asset_name.replace('/', '_')}"
            dl_url = f"https://github.com/{REPO}/releases/download/{TAG}/{urllib.parse.quote(asset_name)}"
            subprocess.run(
                ["curl", "-sL", "--retry", "5", "--retry-delay", "2", "-o", apk_path, dl_url],
                check=True, timeout=600,
            )
            apk = APK(apk_path)
            meta = {
                "package": apk.package,
                "name": apk.application,
                "version": apk.version_name,
                "versionCode": apk.version_code,
            }
        except Exception as e:
            meta = {"error": str(e)[:200]}
        finally:
            if apk_path:
                try:
                    os.remove(apk_path)
                except Exception:
                    pass

        icon = None
        for ic in icons:
            if ic.startswith(d + "."):
                icon = f"icons/{ic}"
                break

        apps.append({
            "code": d,
            "name": meta.get("name"),
            "package": meta.get("package"),
            "version": meta.get("version"),
            "versionCode": meta.get("versionCode"),
            "error": meta.get("error"),
            "icon": icon,
            "files": sorted(f["file"] for f in files),
            "latestFile": latest["file"],
            "latestSize": latest["size"],
            "latestUrl": url,
        })

    with open("/tmp/appsinfo/apps.json", "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=1)

    ok = sum(1 for a in apps if not a.get("error"))
    print(f"apps.json 完成: {ok}/{len(apps)} 应用解析成功", flush=True)

if __name__ == "__main__":
    main()