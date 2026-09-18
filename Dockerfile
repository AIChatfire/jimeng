# jimeng-service 生产镜像
#
# 刻意**不写** `# syntax=docker/dockerfile:1`：那会强制去 Docker Hub 拉 frontend 镜像，
# 受限网络下（内网代理、镜像站只代理 library、出口不稳）一旦拉不到就是整条 build 失败，
# 而本文件并未用到 1.1 的任何特性。少一个外部依赖，少一类失败。
#
# 刻意保持**单进程**（WORKERS=1）——这是架构约束而非保守参数：
#   ① 上下游节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是**进程内**状态，
#      进程数 N 会让限速按 N 倍放大，而那正是上游风控最敏感的维度；
#   ② 任务协调器靠 SQLite 租约选主，多 worker 虽安全但只会互相空转。
# 原因详见 README「扩容前必读」。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：只改代码时不会击穿这一层的缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY gunicorn_conf.py ./

# 非 root 运行；任务库目录必须可写
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data
USER appuser

# 上游单张 2K 图实测约 19-40s，后编辑族更久 —— gunicorn 超时必须覆盖它，
# 默认 30s 会误杀正常请求。协调器在后台推进，请求本身很轻，这里留足余量。
ENV HOST=0.0.0.0 \
    PORT=8200 \
    WORKERS=1 \
    TASK_DB=postgresql+psycopg2://jimeng:jimeng@db:5432/jimeng \
    GUNICORN_TIMEOUT=300

EXPOSE 8200

# 只探测 /healthz（不触上游、不消耗任何积分）。PORT 从环境读，避免与自定义端口脱节。
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request;p=os.environ.get('PORT','8200');sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=5).status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]
