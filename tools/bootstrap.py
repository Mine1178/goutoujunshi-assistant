# -*- coding: utf-8 -*-
"""准备运行所需的本地资源：技能目录与图标。

开源仓库不附带第三方技能正文，也不附带任何赞赏二维码：
1. 首次运行时从上游 MIT 仓库拉取 goutoujunshi 技能；
2. 用 Pillow 画一个简洁的应用图标，供窗口和打包使用。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "goutoujunshi"
ASSETS = ROOT / "assets"
UPSTREAM = "https://github.com/shengjidaguai-china/goutoujunshi.git"
ZIP_URL = "https://codeload.github.com/shengjidaguai-china/goutoujunshi/zip/refs/heads/main"


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}")


def fetch_skill() -> None:
    if (SKILL / "SKILL.md").exists():
        log("技能已存在，跳过下载")
        return
    SKILL.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "goutoujunshi"
        ok = False
        try:
            log("尝试 git clone 上游技能…")
            subprocess.run(
                ["git", "clone", "--depth", "1", UPSTREAM, str(tmp)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            ok = (tmp / "SKILL.md").exists()
        except Exception as exc:
            log(f"git clone 不可用：{exc}")
        if not ok:
            log("改用 GitHub ZIP 下载…")
            req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "goutoujunshi-assistant"})
            data = urllib.request.urlopen(req, timeout=60).read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(Path(td) / "zip")
            roots = [p for p in (Path(td) / "zip").iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError("下载包结构异常")
            tmp = roots[0]
            ok = (tmp / "SKILL.md").exists()
        if not ok:
            raise RuntimeError("没有找到 SKILL.md，请检查网络或上游仓库地址")
        if SKILL.exists():
            shutil.rmtree(SKILL)
        shutil.copytree(tmp, SKILL, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    log(f"技能已就位：{SKILL}")


def make_icon() -> None:
    if (ASSETS / "icon.ico").exists() and (ASSETS / "logo_256.png").exists():
        log("图标已存在，跳过生成")
        return
    from PIL import Image, ImageDraw

    ASSETS.mkdir(parents=True, exist_ok=True)
    size = 1024
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.34), fill=(255, 147, 38, 255))
    # 简洁狗头：耳朵、脸、眼睛、鼻子
    d.ellipse([170, 245, 445, 575], fill=(255, 255, 255, 255))
    d.ellipse([579, 245, 854, 575], fill=(255, 255, 255, 255))
    d.rounded_rectangle([238, 292, 786, 845], radius=250, fill=(255, 255, 255, 255))
    d.ellipse([340, 480, 430, 590], fill=(48, 62, 86, 255))
    d.ellipse([594, 480, 684, 590], fill=(48, 62, 86, 255))
    d.ellipse([447, 592, 577, 706], fill=(48, 62, 86, 255))
    d.arc([430, 660, 594, 790], 20, 160, fill=(48, 62, 86, 255), width=22)
    # 贴边裁掉 2px 抗锯齿脏边
    im = im.crop((2, 2, size - 2, size - 2)).resize((1024, 1024), Image.LANCZOS)
    im.resize((256, 256), Image.LANCZOS).save(ASSETS / "logo_256.png")
    frames = [im.resize((s, s), Image.LANCZOS) for s in (256, 128, 64, 48, 32, 16)]
    frames[0].save(ASSETS / "icon.ico", format="ICO", sizes=[(s, s) for s in (256, 128, 64, 48, 32, 16)], append_images=frames[1:])
    log("图标已生成")


def main() -> int:
    (ROOT / "chat").mkdir(exist_ok=True)
    fetch_skill()
    make_icon()
    log("准备完成。运行 python main.py 启动。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"失败：{exc}")
        raise SystemExit(1)

