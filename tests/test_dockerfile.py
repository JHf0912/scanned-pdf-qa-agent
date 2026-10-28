"""Docker 构建上下文的自检（不依赖 docker 是否安装）。

## 为什么需要这个

`Dockerfile` 写错了有两种表现，**第一种最容易被漏掉**：

1. `docker build` 成功，`docker run` 立刻失败——因为某个运行期必需的目录
   没被 `COPY` 进去。构建阶段完全不报错，你只有在容器里跑起来才会发现；
2. `docker build` 直接失败。

第 1 类正是本项目反复踩的那类缺陷（"不报错、只是不工作"）。
写这份 Dockerfile 时就真的漏了 `config/` 与 `prompts/`——两个都**不是**
可选的：前者是 `config.py` 用 `Path(__file__).parents[2]` 回推的配置位置，
后者是 `ocr/__init__.py` 用 `parents[3]` 回推的提示词位置。

`.dockerignore` 还有个更阴的坑：`*.md` 在 docker 里与 gitignore 同语义，
**匹配任意层级**，于是它把 `prompts/ocr_page.v1.md` 也排除了。

## 这里测什么

不模拟 docker 的完整语义（那需要 docker 本体），只逐条断言**可以静态判定
且一旦错了就一定出问题**的三件事：

- Dockerfile `COPY` 了每一个运行期按相对路径读取的目录；
- 那些目录没有被 `.dockerignore` 的规则排除；
- `.env` 被明确排除（密钥绝不能进镜像层）。

规则的匹配用一份"够用于本仓库布局"的简化实现，写在 `_ignored()` 里，
并对它自己做了自测——一份悄悄失效的检查比没有检查更糟。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

# 运行期按**相对路径**读取的目录。清单来自源码里的实际解析逻辑，
# 不是凭印象列的（每条都标了出处）。
RUNTIME_PATHS = {
    "config": "config.py: PROJECT_ROOT = Path(__file__).parents[2]，"
              "装载 config/default.yaml 与 config/domains/*.yaml",
    "prompts": "ocr/__init__.py::_default_prompt_path()，"
               "parents[3] / 'prompts' / 'ocr_page.v1.md'",
    "src": "全部产品代码",
}


def _ignore_lines() -> list[str]:
    return [
        ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _ignored(rel_path: str) -> bool:
    """简化版 .dockerignore 匹配。

    支持本文件实际用到的形态：

    - 字面目录前缀：`docs/`
    - 扩展名通配：`*.md`（**任意层级**，与 gitignore 同语义）
    - 带目录的通配：`prompts/*.md`（**只匹配该目录下一层**）
    - 否定：`!x`

    不实现 `**`、字符类等高级语法——用到的时候应当先扩这里，
    而不是让它静默失灵。
    """
    ignored = False
    for raw in _ignore_lines():
        negate = raw.startswith("!")
        pat = raw[1:] if negate else raw

        if pat.endswith("/"):                       # 目录：前缀匹配
            hit = rel_path == pat[:-1] or rel_path.startswith(pat)
        elif pat.startswith("*."):                  # 裸扩展名：匹配任意层级
            hit = rel_path.endswith(pat[1:])
        elif "*" in pat:                            # 带目录的通配：按 fnmatch 语义
            hit = fnmatch.fnmatch(rel_path, pat)
        else:                                       # 其余按前缀/全等
            hit = rel_path == pat or rel_path.startswith(pat)

        if hit:
            ignored = not negate
    return ignored


class TestIgnoreRuleMatcher:
    """先测匹配器本身。它错了，下面所有断言都会变成假绿。"""

    def test_directory_rule(self):
        assert _ignored("docs/deployment.md")
        assert not _ignored("config/default.yaml")

    def test_extension_rule_matches_any_depth(self):
        """`*.md` 必须能匹配到 `prompts/ocr_page.v1.md`——这是真实踩过的坑。

        注意这里断言的是**规则命中了**，而不是"最终被忽略"：
        `.dockerignore` 里后面还有一条 `!prompts/*.md` 把它救回来。
        两者要分开测，否则"救回来了"会掩盖"规则确实会误伤"这个事实，
        将来有人删掉那条否定规则时也不会有人发现。
        """
        assert any(
            ln == "*.md" for ln in _ignore_lines()
        ), "缺少 *.md 规则，测试的前提已不成立"
        # 只有 *.md 这一条时，任意层级的 .md 都会被命中
        assert fnmatch.fnmatch("prompts/ocr_page.v1.md", "*.md")
        assert fnmatch.fnmatch("docs/deployment.md", "*.md")

    def test_negation_restores_a_file(self):
        assert not _ignored("README.md")            # 有 !README.md
        assert not _ignored("prompts/ocr_page.v1.md")  # 有 !prompts/*.md


class TestDockerfileCoversRuntimePaths:
    @pytest.mark.parametrize("name", sorted(RUNTIME_PATHS))
    def test_path_is_copied(self, name):
        text = DOCKERFILE.read_text(encoding="utf-8")
        # 允许 `COPY config ./config` 或 `COPY config/ ./config/`
        assert re.search(rf"^COPY\s+{name}/?\s", text, re.MULTILINE), (
            f"Dockerfile 没有 COPY {name}/，但它是运行期必需的：\n"
            f"  {RUNTIME_PATHS[name]}\n"
            f"后果：docker build 会成功，docker run 一启动就失败。"
        )

    @pytest.mark.parametrize("name", sorted(RUNTIME_PATHS))
    def test_path_is_not_dockerignored(self, name):
        assert not _ignored(name), (
            f"{name}/ 被 .dockerignore 排除了，但它必须在构建上下文里。\n"
            f"  {RUNTIME_PATHS[name]}"
        )

    def test_prompt_file_survives_the_md_rule(self):
        """单独钉住提示词：`*.md` 与 `!prompts/*.md` 的交互很容易改坏。"""
        assert not _ignored("prompts/ocr_page.v1.md")
        assert (ROOT / "prompts" / "ocr_page.v1.md").exists()


class TestSecretsNeverEnterTheImage:
    def test_env_is_ignored(self):
        """镜像是分层且不可变的：密钥一旦进过某一层，删掉也捞得出来。"""
        assert _ignored(".env")
        assert _ignored(".env.production")

    def test_env_example_is_kept(self):
        """.env.example 里只有变量名，留着方便容器里对照填写。"""
        assert not _ignored(".env.example")

    def test_dockerfile_never_copies_env(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("COPY"):
                assert ".env" not in line, f"这一行会把密钥带进镜像：{line}"


class TestContainerRunsAsNonRoot:
    def test_has_user_directive(self):
        """本项目处理外来 PDF（不可信输入），容器里不该用 root 跑。"""
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^USER\s+(?!root)", text, re.MULTILINE), (
            "Dockerfile 缺少非 root 的 USER 指令"
        )

    def test_healthcheck_checks_ready_not_status_code(self):
        """`/api/status` 在装载失败时**仍返回 200**。

        只看状态码的健康检查会把"一份产物都装不上"判成健康——
        那恰恰是最该报警的状态。
        """
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "HEALTHCHECK" in text
        assert "ready" in text, (
            "健康检查必须读 /api/status 的 ready 字段；"
            "只判断 HTTP 状态码会把装载失败误判成健康"
        )
