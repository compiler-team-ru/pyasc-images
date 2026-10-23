#!/usr/bin/env bash
set -e
if [ -f /usr/local/Ascend/cann/set_env.sh ]; then
    source /usr/local/Ascend/cann/set_env.sh
elif [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then
    source /usr/local/Ascend/ascend-toolkit/set_env.sh
else
    source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
fi
export PATH="/opt/python/bin:$PATH"
export LD_LIBRARY_PATH="/opt/python/lib:${ASCEND_HOME_PATH}/tools/simulator/Ascend950PR_9599/lib:${LD_LIBRARY_PATH:-}"
exec "$@"
