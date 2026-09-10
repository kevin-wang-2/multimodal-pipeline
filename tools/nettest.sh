#!/usr/bin/env bash
# nettest.sh — 从 Mac 发起，测到另一台机器的点对点吞吐
#
# 只依赖两端都有: ssh / cat / head（Windows 用 Git Bash 自带的即可）
# 上传: Mac -> 远端        下载: 远端 -> Mac
#
# 用法:
#   tools/nettest.sh <ssh目标> [每次MB=200] [并发=4]
# 例:
#   tools/nettest.sh seanartech-lab
#   tools/nettest.sh lenovo@192.168.71.201 300
#   tools/nettest.sh 100.64.0.2 200 8
set -u

DEST="${1:-}"; MB="${2:-200}"; N="${3:-4}"
if [ -z "$DEST" ]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$DEST")
BYTES=$((MB * 1048576))
now() { python3 -c 'import time;print(time.time())'; }
rate() { awk -v s="$1" -v m="$2" 'BEGIN{printf "%6.1f MB/s  (%5.0f Mbit/s)", m/s, m*8/s}'; }

echo "目标: $DEST   每次 ${MB} MiB   并发 ${N}"
echo

# ---- 预热（避免 TCP 慢启动污染第一发）----
head -c $((1048576)) /dev/zero | "${SSH[@]}" 'cat > /dev/null' 2>/dev/null || {
  echo "!! 无法通过 ssh 连到 $DEST（检查别名/端口/免密）"; exit 1; }
"${SSH[@]}" "head -c 1048576 /dev/zero" > /dev/null 2>&1 || true

run_upload() {   # $1 = 并发数
  local n=$1 t0 t1
  t0=$(now)
  for _ in $(seq 1 "$n"); do head -c "$BYTES" /dev/zero | "${SSH[@]}" 'cat > /dev/null' & done
  wait; t1=$(now)
  awk -v a="$t0" -v b="$t1" -v m=$((MB*n)) 'BEGIN{print b-a, m}'
}
run_download() { # $1 = 并发数
  local n=$1 t0 t1
  t0=$(now)
  for _ in $(seq 1 "$n"); do "${SSH[@]}" "head -c $BYTES /dev/zero" > /dev/null & done
  wait; t1=$(now)
  awk -v a="$t0" -v b="$t1" -v m=$((MB*n)) 'BEGIN{print b-a, m}'
}

printf "%-22s %9s %8s   %s\n" "方向(并发)" "用时" "数据量" "速率"
r=$(run_upload 1);   printf "%-22s %8.2fs %6sMiB   %s\n" "上传 x1"   "$(echo $r|cut -d' ' -f1)" "$(echo $r|cut -d' ' -f2)" "$(rate $(echo $r|cut -d' ' -f1) $(echo $r|cut -d' ' -f2))"
r1_up=$(echo $r | cut -d' ' -f1)
r=$(run_download 1); printf "%-22s %8.2fs %6sMiB   %s\n" "下载 x1"   "$(echo $r|cut -d' ' -f1)" "$(echo $r|cut -d' ' -f2)" "$(rate $(echo $r|cut -d' ' -f1) $(echo $r|cut -d' ' -f2))"
r=$(run_upload "$N");   printf "%-22s %8.2fs %6sMiB   %s\n" "上传 x$N"   "$(echo $r|cut -d' ' -f1)" "$(echo $r|cut -d' ' -f2)" "$(rate $(echo $r|cut -d' ' -f1) $(echo $r|cut -d' ' -f2))"
rN_up=$(echo $r | cut -d' ' -f1)
r=$(run_download "$N"); printf "%-22s %8.2fs %6sMiB   %s\n" "下载 x$N"   "$(echo $r|cut -d' ' -f1)" "$(echo $r|cut -d' ' -f2)" "$(rate $(echo $r|cut -d' ' -f1) $(echo $r|cut -d' ' -f2))"

echo
awk -v a="$r1_up" -v b="$rN_up" -v n="$N" 'BEGIN{
  if (b <= 0) exit
  sp = a/b
  if (sp > n*0.7) print "→ 并发能线性叠加：**延迟/窗口受限**（不是硬带宽封顶）"
  else if (sp > 1.6) print "→ 并发部分叠加（" sprintf("%.2f", sp) "x）：介于两者之间，可能有轻微拥塞"
  else print "→ 并发完全不叠加：**硬带宽封顶**（某一段链路速率不够）"
}'
