"""启动入口：本地运行博客阅读器。

用法:
    python run.py                # 默认 127.0.0.1:8000
    BLOGREADER_PORT=9000 python run.py

启动后浏览器打开 http://127.0.0.1:8000 即可使用。
"""
import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("BLOGREADER_HOST", "127.0.0.1")
    port = int(os.environ.get("BLOGREADER_PORT", "8000"))
    # 用 import 字符串以支持热重载；这里默认关闭 reload，个人自用更稳
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
