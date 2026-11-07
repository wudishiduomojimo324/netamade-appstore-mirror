#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像仓库增量同步（在 GitHub Actions runner 上执行）

流程:
  1) 读源仓库 appstore 分支的 git tree，列出全部 apks/ 下的 APK 与图标
  2) 读镜像仓库 Release 附件，按 `dir__file` 命名对比出缺失项
  3) 缺失 APK: 从 media.githubusercontent.com 下载（runner 内网极快）
     → gh release upload 到镜像 Release
  4) 缺失图标: 从 raw.githubusercontent.com 下载 → 提交到 icons/
  5) 用真实文件大小重写 mirror-manifest.json

幂等: 只处理镜像库没有的文件，重复运行无副作用。
"""
import json, os, re, subprocess, sys, urllib.request, urllib.parse

MIRROR = os.environ.get("MIRROR_REPO", "wudishiduomojimo324/netamade-appstore-mirror")
SRC = os.environ.get("SRC_REPO", "z0brk/netamade-releases")
SRC_BRANCH = os.environ.get("SRC_BRANCH", "appstore")
TAG = os.environ.get("RELEASE_TAG", "appstore-202610")
TOKEN = os.environ.get("GH_TOKEN", "")
WORK = "/tmp/sync"
REPO_DIR = os.environ.get("GITHUB_WORKSPACE", ".")

IMAGE_EXT = (".png", ".webp", ".jpg", ".jpeg")


def api(path, repo=MIRROR, raw=False):
    url = f"https://api.github.com/repos/{repo}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
    req.add_header("Cache-Control", "no-cache")
    with urllib.request.urlopen(req, timeout=90) as r:
        body = r.read()
        return body.decode("utf-8") if raw else json.loads(body.decode())


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def main():
    os.makedirs(WORK, exist_ok=True)
    print(f"源: {SRC}@{SRC_BRANCH}  →  镜像: {MIRROR}@{TAG}", flush=True)

    # 1) 源仓库树
    tree = api(f"git/trees/{SRC_BRANCH}?recursive=1", repo=SRC)["tree"]
    apks, icons = {}, {}
    for t in tree:
        p = t.get("path", "")
        if not p.startswith("apks/") or t.get("type") != "blob":
            continue
        parts = p.split("/")
        if len(parts) < 3:
            continue
        d, f = parts[1], parts[2]
        if f.lower().endswith(IMAGE_EXT):
            icons[f"icons/{d}{os.path.splitext(f)[1].lower()}"] = p
        else:
            apks[f"{d}__{f}"] = p
    print(f"源仓库: {len(apks)} 个 APK, {len(icons)} 个图标", flush=True)

    # 2) 镜像 Release 已有附件
    rel = api(f"releases/tags/{urllib.parse.quote(TAG)}")
    have = {a["name"] for a in rel.get("assets", [])}
    print(f"镜像 Release 现有 {len(have)} 个附件", flush=True)

    # 3) 缺失 APK 下载 + 上传
    missing = {n: p for n, p in apks.items() if n not in have}
    print(f"需要补 {len(missing)} 个 APK", flush=True)
    real_sizes = {}
    uploaded, failed = [], []
    for name, path in sorted(missing.items()):
        url = f"https://media.githubusercontent.com/media/{SRC}/{SRC_BRANCH}/{path}"
        dst = os.path.join(WORK, name)
        try:
            run(["curl", "-sL", "--retry", "5", "--retry-delay", "2", "-o", dst, url], timeout=900)
            size = os.path.getsize(dst)
            if size < 10240:  # LFS 指针/错误页，不是真 APK
                raise RuntimeError(f"下载内容异常，仅 {size} 字节（疑似 LFS 指针）")
            real_sizes[name] = size
            # gh release upload 必须用 tag 名（用 release id 会报 release not found）
            run(["gh", "release", "upload", TAG, dst, "--repo", MIRROR, "--clobber"], timeout=900)
            uploaded.append((name, size))
            print(f"  ✓ {name} ({size/1048576:.1f}MB)", flush=True)
        except Exception as e:
            failed.append((name, str(e)[:150]))
            print(f"  ✗ {name}: {str(e)[:150]}", flush=True)
        finally:
            if os.path.exists(dst):
                os.remove(dst)

    # 已存在附件的真实大小（写 manifest 用）
    for a in rel.get("assets", []):
        if a["name"] in apks:
            real_sizes[a["name"]] = a["size"]

    # 4) 缺失图标下载到仓库 icons/
    icon_added = 0
    icon_dst_dir = os.path.join(REPO_DIR, "icons")
    os.makedirs(icon_dst_dir, exist_ok=True)
    for target, path in sorted(icons.items()):
        out = os.path.join(REPO_DIR, target)
        if os.path.exists(out):
            continue
        url = f"https://raw.githubusercontent.com/{SRC}/{SRC_BRANCH}/{path}"
        try:
            run(["curl", "-sL", "--retry", "3", "-o", out, url], timeout=180)
            if os.path.getsize(out) < 100:
                os.remove(out)
                raise RuntimeError("文件内容过小")
            icon_added += 1
        except Exception as e:
            print(f"  ✗ icon {target}: {str(e)[:100]}", flush=True)
            if os.path.exists(out):
                os.remove(out)
    print(f"图标新增 {icon_added} 个", flush=True)

    # 5) 重写 mirror-manifest.json（全量，含真实大小）
    manifest = []
    for name, path in sorted(apks.items()):
        d, f = name.split("__", 1)
        manifest.append({"dir": d, "file": f, "size": real_sizes.get(name, 0)})
    with open(os.path.join(REPO_DIR, "mirror-manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=1)

    print(f"\n同步完成: 上传 {len(uploaded)} 个 APK, 失败 {len(failed)} 个, 图标 {icon_added} 个", flush=True)
    print(f"manifest 条目: {len(manifest)}", flush=True)
    if failed:
        for n, e in failed:
            print(f"  FAILED {n}: {e}", flush=True)
    # 写结果给 workflow 后续步骤
    with open(os.path.join(WORK, "result.json"), "w") as fp:
        json.dump({"uploaded": len(uploaded), "failed": failed, "icons": icon_added}, fp)


if __name__ == "__main__":
    main()
