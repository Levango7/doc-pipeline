#!/bin/bash
# Doc-Pipeline 健康检查脚本
# 用法: bash healthcheck.sh [port]
# 返回: 0 = 健康, 1 = 不健康

PORT=${1:-8910}
URL="http://127.0.0.1:${PORT}/health"

# 检查 HTTP 200
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${URL}" 2>/dev/null)
if [ "${HTTP_CODE}" != "200" ]; then
    echo "UNHEALTHY: HTTP ${HTTP_CODE:-timeout}"
    exit 1
fi

# 检查 JSON 状态
STATUS=$(curl -s --max-time 5 "${URL}" 2>/dev/null | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
if [ "${STATUS}" = "ok" ]; then
    echo "HEALTHY: status=ok"
    exit 0
else
    echo "UNHEALTHY: status=${STATUS}"
    exit 1
fi