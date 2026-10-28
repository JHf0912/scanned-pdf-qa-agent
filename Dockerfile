# docqa 运行镜像
#
# ⚠️ 分发前必读（AGPL）
# ------------------------------------------------------------------------------
# 本镜像里包含 PyMuPDF，它是 **AGPL-3.0**（双许可）。这意味着：
#
#   - 你**分发这个镜像**（推到 registry、发给客户）→ AGPL 要求整个组合作品
#     按 AGPL 开源，或向 Artifex 购买商业许可，或换成宽松许可的 PDF 库；
#   - 你把它**作为网络服务**给别人用 → AGPL 第 13 条要求向使用者提供对应源码；
#   - 只在内部使用、不分发也不对外提供服务 → 通常不触发上述义务。
#
# 本仓库自己的代码是 MIT（见 LICENSE），但那**不能**覆盖依赖的许可。
# 这不是法律意见，是提醒你先去读 AGPL 原文或咨询法务。
# 详见 docs/deployment.md 与 docs/scalability.md 第六节。
# ------------------------------------------------------------------------------

FROM python:3.12-slim

# 运行期依赖：
#   libgl1 / libglib2.0-0 —— PyMuPDF 与 Pillow 的动态库依赖
#   fonts-noto-cjk        —— 无中文字体时合成页图会变成方块（doctor 探针也受影响）
# 装完立刻清 apt 缓存：否则这些 .deb 会永久留在镜像层里。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 用 uv 而不是 pip：uv.lock 已入库，装出来的版本与开发机一致。
# 这份可复现性在镜像里比在开发机上更重要——镜像发出去就不再受你控制。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只拷依赖清单再装依赖：改一行源码不该让整个依赖层失效。
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# ⚠️ 下面这三个目录**不是可选的**，它们在运行期按相对路径解析：
#
#   config/   ← config.py: PROJECT_ROOT = Path(__file__).parents[2]
#               装载 config/default.yaml 与 config/domains/*.yaml；
#               缺了它，`serve` 一启动就 ConfigError（找不到配置文件）。
#   prompts/  ← ocr/__init__.py::_default_prompt_path()
#               parents[3] / "prompts" / "ocr_page.v1.md"；
#               缺了它，OCR 引擎构造时就读不到提示词。
#
# 这两个路径都是**从源码位置回推仓库根**，不是从工作目录，
# 所以容器里的目录结构必须与仓库一致。第一次写这份 Dockerfile 时
# 漏了它们——`docker build` 会成功，`docker run` 立刻失败。
COPY config ./config
COPY prompts ./prompts

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# --all-extras 是必需的：不带它 `uv sync` 会把 eval 这组 extras 卸掉。
RUN uv sync --all-extras --frozen --no-dev

# 非 root 运行。容器里的 root 一旦被利用，逃逸面会大得多；
# 而本项目会处理**外来的 PDF**，那正是不可信输入。
RUN useradd --create-home --uid 10001 docqa \
    && mkdir -p /app/data/raw /app/data/derived /app/out \
    && chown -R docqa:docqa /app
USER docqa

# 只绑到容器网卡，由 `-p` 决定对外暴露。
# 默认仍是 127.0.0.1 的话，容器外根本连不上——这里必须显式 0.0.0.0。
EXPOSE 8000

# 健康检查问的是 `/api/status` 的 **`ready` 字段**，不是 HTTP 状态码。
#
# 这个区别很实质：`/api/status` 在装载失败时**仍然返回 200**，
# 只是 body 里 `ready: false`。只看状态码的健康检查会把"进程活着但
# 一份产物都装载不上"判成健康——那正是最该报警的状态。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,sys,urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4); \
sys.exit(0 if json.load(r).get('ready') else 1)"

ENTRYPOINT ["docqa"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
