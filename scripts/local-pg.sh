#!/usr/bin/env bash
# 本地开发用 PostgreSQL —— **独立实例**，不碰你机器上已有的那个集群。
#
# 为什么用独立实例而不是直接用 5432 上那个：
#   1. 不知道也不该去猜现有集群的超级用户密码；
#   2. 开发库要能被随意 drop/create，不该和别人的数据混在一个实例里；
#   3. 端口分开（5433）⇒ 两边互不干扰，随时可关掉。
#
# 二进制复用本机已装的 PostgreSQL 15（EDB 安装包，在 /Library/PostgreSQL/15/bin）。
# 若你装了别的版本，用 `PGBIN=... scripts/local-pg.sh ...` 覆盖即可。
#
# 用法：
#   scripts/local-pg.sh init     初始化数据目录（幂等；已存在则跳过）
#   scripts/local-pg.sh start    启动（幂等）
#   scripts/local-pg.sh stop     停止
#   scripts/local-pg.sh status   状态 + 连接串
#   scripts/local-pg.sh dsn      只打印两个 DSN（可直接 export）
#   scripts/local-pg.sh destroy  ⚠️ 删除数据目录（不可逆）

set -euo pipefail

PGBIN="${PGBIN:-/Library/PostgreSQL/15/bin}"
PGDATA="${PGDATA:-$HOME/pgdata/jimeng}"
PGPORT="${PGPORT:-5433}"
PGUSER_LOCAL="${PGUSER_LOCAL:-jimeng}"
PGPASSWORD_LOCAL="${PGPASSWORD_LOCAL:-jimeng}"
DBS="${DBS:-jimeng jimeng_test}"
# ⚠️ 日志放在数据目录**同级**（`$PGDATA.xxx.log`），不能放进 $PGDATA 里面 ——
# initdb 要求目标目录**必须为空**，往里面写一个 initdb.log 就会直接失败
# （报 "directory exists but is not empty"，第一次踩过）。
INITDB_LOG="$PGDATA.initdb.log"
LOGFILE="$PGDATA.server.log"

die() { echo "错误：$*" >&2; exit 1; }
info() { echo "==> $*"; }

need_bin() {
  [ -x "$PGBIN/$1" ] || die "找不到 $PGBIN/$1 —— 用 PGBIN=... 指定 PostgreSQL 的 bin 目录"
}

# 集群是否在跑（用 pg_ctl status 判定，别用 ps —— 本机 ps 被沙箱禁掉）
running() {
  "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1
}

cmd_init() {
  need_bin initdb
  if [ -s "$PGDATA/PG_VERSION" ]; then
    info "数据目录已存在，跳过初始化：$PGDATA"
    return 0
  fi
  mkdir -p "$PGDATA"
  local pwfile
  pwfile="$(mktemp)"
  # 用文件传密码，避免出现在 ps 的命令行里
  printf '%s' "$PGPASSWORD_LOCAL" > "$pwfile"
  info "初始化集群：$PGDATA（超级用户 $PGUSER_LOCAL，端口 $PGPORT）"
  "$PGBIN/initdb" -D "$PGDATA" -U "$PGUSER_LOCAL" --pwfile="$pwfile" \
    --encoding=UTF8 --locale=C --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
    >"$INITDB_LOG" 2>&1 || { rm -f "$pwfile"; die "initdb 失败，见 $INITDB_LOG"; }
  rm -f "$pwfile"
  info "初始化完成"
}

cmd_start() {
  need_bin pg_ctl
  [ -s "$PGDATA/PG_VERSION" ] || die "数据目录未初始化，先跑：$0 init"
  if running; then
    info "已在运行（端口 $PGPORT）"
  else
    info "启动中…"
    # 只监听回环：开发库不该暴露到局域网
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$LOGFILE" \
      -o "-p $PGPORT -c listen_addresses=127.0.0.1" -w start >/dev/null \
      || die "启动失败，见 $LOGFILE"
    info "已启动"
  fi
  cmd_mkdb
}

cmd_mkdb() {
  need_bin psql
  local -a psqlq=("$PGBIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_LOCAL"
                  -v ON_ERROR_STOP=1 -tA)
  export PGPASSWORD="$PGPASSWORD_LOCAL"
  for db in $DBS; do
    # 用 psql 的 -tA 输出做判断，**不用 grep** —— 沙箱里 grep 可能被垫片替换而静默给错结果
    local exists
    exists="$("${psqlq[@]}" -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$db'")"
    if [ "$exists" = "1" ]; then
      info "库 $db 已存在"
    else
      "${psqlq[@]}" -d postgres -c "CREATE DATABASE \"$db\" OWNER \"$PGUSER_LOCAL\"" >/dev/null
      info "已创建库 $db"
    fi
  done
  unset PGPASSWORD
}

cmd_stop() {
  need_bin pg_ctl
  if running; then
    "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null && info "已停止"
  else
    info "未在运行"
  fi
}

cmd_status() {
  if running; then
    info "运行中：$PGDATA（端口 $PGPORT）"
  else
    info "未运行：$PGDATA"
  fi
  cmd_dsn
}

cmd_dsn() {
  echo "TASK_DB=postgresql+psycopg2://$PGUSER_LOCAL:$PGPASSWORD_LOCAL@127.0.0.1:$PGPORT/jimeng"
  echo "TEST_DATABASE_URL=postgresql+psycopg2://$PGUSER_LOCAL:$PGPASSWORD_LOCAL@127.0.0.1:$PGPORT/jimeng_test"
}

cmd_destroy() {
  need_bin pg_ctl
  running && cmd_stop
  info "⚠️ 即将删除 $PGDATA"
  rm -rf "$PGDATA"
  info "已删除"
}

case "${1:-}" in
  init)    cmd_init ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  dsn)     cmd_dsn ;;
  destroy) cmd_destroy ;;
  *) echo "用法：$0 {init|start|stop|status|dsn|destroy}" >&2; exit 2 ;;
esac
