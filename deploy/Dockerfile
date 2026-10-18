# AI 管家 Agent 生态体系 —— 统一镜像
# 计划书依据：三、P0-1「部署云原生容器环境，统一 Dockerfile、资源限制、启动模板」
# 说明：四环境共用同一镜像，差异全部由 --env-file 提供（见 ai_butler/core/environments.py）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ai_butler /app/ai_butler
COPY deploy /app/deploy

# 容器内以非 root 运行
RUN useradd --create-home --uid 10001 aibutler && chown -R aibutler:aibutler /app
USER aibutler

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=6 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "ai_butler.server:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
