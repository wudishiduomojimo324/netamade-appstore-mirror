#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像仓库增量同步（在 GitHub Actions runner 上执行）

关键: 源库 apks/ 里的文件是 git-LFS 指针，真实内容在
      media.githubusercontent.com。GitHub Release asset 带 digest(sha256)，
      LFS 指针里带 oid(sha256) —— 两者直接比对即可精确判断"内容是否更新"，
      无需下载 4GB 文件。

处理三类:
  NEW      镜像没有            -> 下载 + 上传
  CHANGED  同名但 oid 变了     -> 删旧 asset + 下载 + 上传
  SAME     一致                -> 跳过

另外:
  - 新增图标 -> icons/
  - 重写 mirror-manifest.json（全量，带真实 size）
  - 保留镜像库自有资产（不在源库里的，绝不删）
  - 按 locklist.json 跳过"带锁/需授权"应用（下载了也用不了）

幂等: 重复运行只处理差异。
"""
import base64
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

MIRROR = os.environ.get("MIRROR_REPO", "wudishiduomojimo324/netamade-appstore-mirror")
SRC = os.environ.get("SRC_REPO", "z0brk/netamade-releases")
SRC_BRANCH = os.environ.get("SRC_BRANCH", "appstore")
TAG = os.environ.get("RELEASE_TAG", "appstore-202610")
TOKEN = os.environ.get("GH_TOKEN", "")
WORK = "/tmp/sync"
REPO_DIR = os.environ.get("GITHUB_WORKSPACE", ".")

IMAGE_EXT = (".png", ".webp", ".jpg", ".jpeg")


def api(path, repo=None, timeout=90):
    url = f"https://api.github.com/repos/{repo or MIRROR}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Cache-Control", "no-cache")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def api_delete(path):
    url = f"https://api.github.com/repos/{MIRROR}/{path}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def load_locklist():
    """读取仓库里的 locklist.json（带锁应用黑名单）。

    支持两种格式:
      {"blocked": ["dir", ...]}          按应用目录
      {"blockedFiles": ["dir__file"]}    按具体文件
    文件不存在 = 不过滤。
    """
    p = os.path.join(REPO_DIR, "locklist.json")
    if not os.path.exists(p):
        return set(), set()
    try:
        d = json.load(open(p, encoding="utf-8"))
        return set(d.get("blocked", [])), set(d.get("blockedFiles", []))
    except Exception as e:
        print(f"  ! locklist.json 解析失败: {e}", flush=True)
        return set(), set()


def lfs_oid(blob_sha):
    """读 LFS 指针内容，返回 (oid, size)。非 LFS 返回 (None, None)。"""
    try:
        b = api(f"git/blobs/{blob_sha}", repo=SRC, timeout=60)
        txt = base64.b64decode(b["content"]).decode("utf-8", "ignore")
        oid = None
        size = None
        for line in txt.splitlines():
            if line.startswith("oid sha256:"):
                oid = line.split(":", 1)[1].strip()
            elif line.startswith("size "):
                try:
                    size = int(line.split()[1])
                except Exception:
                    pass
        return oid, size
    except Exception as e:
        print(f"  ! 读指针失败 {blob_sha[:8]}: {str(e)[:80]}", flush=True)
        return None, None


def main():
    os.makedirs(WORK, exist_ok=True)
    print(f"源: {SRC}@{SRC_BRANCH}  →  镜像: {MIRROR}@{TAG}", flush=True)

    # 1) 源库树
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
            apks[f"{d}__{f}"] = {"path": p, "sha": t["sha"]}
    print(f"源库: {len(apks)} 个 APK, {len(icons)} 个图标", flush=True)

    # 2) 带锁过滤
    blocked_dirs, blocked_files = load_locklist()
    if blocked_dirs or blocked_files:
        before = len(apks)
        skipped = []
        for name in list(apks.keys()):
            d = name.split("__", 1)[0]
            if d in blocked_dirs or name in blocked_files:
                skipped.append(name)
                del apks[name]
        print(f"带锁过滤: 跳过 {before - len(apks)} 个", flush=True)
        for n in skipped:
            print(f"  - 跳过(带锁) {n}", flush=True)

    # 3) 镜像 Release 附件（含 digest）
    rel = api(f"releases/tags/{urllib.parse.quote(TAG)}")
    have = {}
    for a in rel.get("assets", []):
        have[a["name"]] = {
            "id": a["id"],
            "size": a["size"],
            "digest": (a.get("digest") or "").replace("sha256:", ""),
        }
    print(f"镜像 Release: {len(have)} 个附件", flush=True)

    # 4) 逐个读源库 LFS oid，分类
    print("读取源库 LFS 指针（判断内容是否更新）...", flush=True)
    todo = []
    src_meta = {}
    for i, (name, info) in enumerate(sorted(apks.items()), 1):
        oid, lfs_size = lfs_oid(info["sha"])
        src_meta[name] = {"path": info["path"], "oid": oid, "lfsSize": lfs_size}
        if name not in have:
            todo.append((name, info["path"], "NEW", None))
        elif oid and have[name]["digest"] and oid != have[name]["digest"]:
            todo.append((name, info["path"], "CHANGED", have[name]["id"]))
        if i % 30 == 0:
            print(f"  ...{i}/{len(apks)}", flush=True)

    n_new = sum(1 for t in todo if t[2] == "NEW")
    n_chg = sum(1 for t in todo if t[2] == "CHANGED")
    print(f"待处理: 新增 {n_new}, 内容更新 {n_chg}, 跳过 {len(apks)-len(todo)}", flush=True)

    # 5) 下载 + 上传
    real_sizes = {n: have[n]["size"] for n in have}
    uploaded, failed = [], []
    for name, path, reason, old_id in todo:
        url = f"https://media.githubusercontent.com/media/{SRC}/{SRC_BRANCH}/{path}"
        dst = os.path.join(WORK, name)
        try:
            if old_id:
                try:
                    api_delete(f"releases/assets/{old_id}")
                except Exception as e:
                    print(f"  ! 删旧 {name} 失败: {str(e)[:80]}", flush=True)

            run(["curl", "-sL", "--retry", "5", "--retry-delay", "2", "-o", dst, url], timeout=900)
            size = os.path.getsize(dst)
            if size < 10240:
                raise RuntimeError(f"下载内容仅 {size} 字节（疑似 LFS 指针/错误页）")
            real_sizes[name] = size
            run(["gh", "release", "upload", TAG, dst, "--repo", MIRROR, "--clobber"], timeout=900)
            uploaded.append((name, reason, size))
            print(f"  ✓ [{reason}] {name} ({size/1048576:.1f}MB)", flush=True)
        except Exception as e:
            failed.append((name, reason, str(e)[:150]))
            print(f"  ✗ [{reason}] {name}: {str(e)[:150]}", flush=True)
        finally:
            if os.path.exists(dst):
                os.remove(dst)

    # 6) 图标
    icon_added = 0
    os.makedirs(os.path.join(REPO_DIR, "icons"), exist_ok=True)
    for target, path in sorted(icons.items()):
        out = os.path.join(REPO_DIR, target)
        if os.path.exists(out):
            continue
        url = f"https://raw.githubusercontent.com/{SRC}/{SRC_BRANCH}/{path}"
        try:
            run(["curl", "-sL", "--retry", "3", "-o", out, url], timeout=180)
            if os.path.getsize(out) < 100:
                os.remove(out)
                raise RuntimeError("内容过小")
            icon_added += 1
        except Exception as e:
            print(f"  ✗ icon {target}: {str(e)[:80]}", flush=True)
            if os.path.exists(out):
                os.remove(out)
    print(f"图标新增 {icon_added} 个", flush=True)

    # 7) 重写 mirror-manifest.json
    manifest = []
    for name, meta in sorted(apks.items()):
        d, f = name.split("__", 1)
        size = real_sizes.get(name) or meta.get("lfsSize") or 0
        manifest.append({"dir": d, "file": f, "size": size})
    with open(os.path.join(REPO_DIR, "mirror-manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=1)

    print(f"\n同步完成: 上传 {len(uploaded)} 个（新 {n_new}/更新 {n_chg}），失败 {len(failed)}，图标 {icon_added}", flush=True)
    print(f"manifest 条目: {len(manifest)}", flush=True)
    for n, r, e in failed:
        print(f"  FAILED [{r}] {n}: {e}", flush=True)

    with open(os.path.join(WORK, "result.json"), "w") as fp:
        json.dump({"uploaded": len(uploaded), "new": n_new, "changed": n_chg,
                   "failed": failed, "icons": icon_added,
                   "blocked": sorted(blocked_dirs | blocked_files)}, fp)


if __name__ == "__main__":
    main()
