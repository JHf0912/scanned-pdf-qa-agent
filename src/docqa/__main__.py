"""让 `python -m docqa` 与 `docqa` 命令走同一条入口。

存在的意义：控制台脚本的入口由 `pyproject.toml` 的 `[project.scripts]` 决定，
而 `if __name__ == "__main__"` 对控制台脚本**不会执行**。
所以 `cli.py` 里的 `main()` 一度是死代码——异常处理与退出码全部失效。

两条入口都指向 `main()`，行为才一致：
- `docqa ...`            → project.scripts → `docqa.cli:main`
- `python -m docqa ...`  → 本文件         → `docqa.cli:main`
"""

from .cli import main

if __name__ == "__main__":
    main()
