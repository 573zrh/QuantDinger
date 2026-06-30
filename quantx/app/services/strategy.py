"""
QuantX 策略生命周期管理

创建、查询、更新、删除策略，以及策略状态机管理。
参考 QuantDinger strategy.py，针对传统市场（股票/期货）精简实现。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.db import fetch_one, fetch_all, get_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 允许的市场类型
VALID_MARKETS = {"CNStock", "HKStock", "USStock", "CNFutures"}

# 允许的策略类型
VALID_STRATEGY_TYPES = {"indicator", "script"}

# 策略状态机：合法的状态转换
# draft → ready → running → stopped → draft（循环）
# error 可从任何状态转入
VALID_STATUSES = {"draft", "ready", "running", "stopped", "error"}
STATUS_TRANSITIONS = {
    "draft": {"ready", "error"},
    "ready": {"running", "error"},
    "running": {"stopped", "error"},
    "stopped": {"draft", "ready", "error"},
    "error": {"draft", "ready", "running", "stopped", "error"},
}

# 可编辑状态：只有这些状态的策略允许修改
EDITABLE_STATUSES = {"draft", "stopped", "error"}


def create_strategy(
    user_id: int,
    name: str,
    market: str,
    symbol: str,
    **kwargs,
) -> Optional[Dict[str, Any]]:
    """
    创建新策略

    Args:
        user_id: 用户 ID
        name: 策略名称
        market: 市场（CNStock/HKStock/USStock/CNFutures）
        symbol: 品种代码
        **kwargs: 可选字段（timeframe, strategy_type, description, indicator_code, params）

    Returns:
        新策略字典，失败返回 None
    """
    # 参数验证
    name = (name or "").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    if len(name) > 200:
        raise ValueError("策略名称不能超过 200 个字符")

    market = (market or "").strip()
    if market not in VALID_MARKETS:
        raise ValueError(f"市场类型无效，必须是 {', '.join(sorted(VALID_MARKETS))} 之一")

    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("品种代码不能为空")

    strategy_type = kwargs.get("strategy_type", "indicator")
    if strategy_type not in VALID_STRATEGY_TYPES:
        raise ValueError(f"策略类型无效，必须是 {', '.join(sorted(VALID_STRATEGY_TYPES))} 之一")

    timeframe = kwargs.get("timeframe", "1D")
    description = kwargs.get("description", "")
    indicator_code = kwargs.get("indicator_code", "")

    # params 字段使用 JSON 存储
    import json
    params = kwargs.get("params", {})
    params_json = json.dumps(params) if isinstance(params, dict) else "{}"

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO qd_strategies
                    (user_id, name, description, market, symbol, timeframe,
                     status, strategy_type, indicator_code, params, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s::jsonb, NOW(), NOW())
                RETURNING id, user_id, name, description, market, symbol, timeframe,
                          status, strategy_type, indicator_code, params, created_at, updated_at
                """,
                (user_id, name, description, market, symbol, timeframe,
                 strategy_type, indicator_code, params_json),
            )
            row = cursor.fetchone()
            conn.commit()
            if row:
                cols = [desc[0] for desc in cursor.description]
                result = dict(zip(cols, row))
                logger.info(f"策略创建成功: {name} (id={result.get('id')}, user={user_id})")
                return result
            return None
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"创建策略失败: {e}")
        return None


def get_strategy(strategy_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    获取策略详情

    Args:
        strategy_id: 策略 ID
        user_id: 若指定则只返回该用户的策略

    Returns:
        策略字典，不存在或无权访问返回 None
    """
    try:
        if user_id is not None:
            return fetch_one(
                "SELECT * FROM qd_strategies WHERE id = %s AND user_id = %s",
                (strategy_id, user_id),
            )
        return fetch_one(
            "SELECT * FROM qd_strategies WHERE id = %s",
            (strategy_id,),
        )
    except Exception as e:
        logger.error(f"获取策略失败: {e}")
        return None


def list_strategies(
    user_id: int,
    status: Optional[str] = None,
    market: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    列出用户策略

    Args:
        user_id: 用户 ID
        status: 按状态过滤（可选）
        market: 按市场过滤（可选）

    Returns:
        策略列表
    """
    conditions = ["user_id = %s"]
    params: list = [user_id]

    if status and status in VALID_STATUSES:
        conditions.append("status = %s")
        params.append(status)

    if market and market in VALID_MARKETS:
        conditions.append("market = %s")
        params.append(market)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, user_id, name, description, market, symbol, timeframe,
               status, strategy_type, created_at, updated_at
        FROM qd_strategies
        WHERE {where}
        ORDER BY updated_at DESC
    """
    try:
        return fetch_all(sql, tuple(params))
    except Exception as e:
        logger.error(f"列出策略失败: {e}")
        return []


def update_strategy(
    strategy_id: int,
    user_id: int,
    **kwargs,
) -> Optional[Dict[str, Any]]:
    """
    更新策略

    只有 draft / stopped / error 状态的策略可编辑。
    可更新字段: name, description, market, symbol, timeframe, strategy_type, indicator_code, params

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID（只能编辑自己的策略）
        **kwargs: 要更新的字段

    Returns:
        更新后的策略字典，失败返回 None
    """
    # 检查策略是否存在且属于当前用户
    strategy = get_strategy(strategy_id, user_id)
    if not strategy:
        raise ValueError("策略不存在或无权访问")

    current_status = strategy.get("status", "draft")
    if current_status not in EDITABLE_STATUSES:
        raise ValueError(f"当前状态 '{current_status}' 不允许编辑，请先停止策略")

    allowed_fields = {
        "name", "description", "market", "symbol", "timeframe",
        "strategy_type", "indicator_code", "params",
    }
    updates = []
    values = []

    import json
    for field_name, value in kwargs.items():
        if field_name not in allowed_fields:
            continue
        if field_name == "market" and value not in VALID_MARKETS:
            raise ValueError(f"市场类型无效: {value}")
        if field_name == "strategy_type" and value not in VALID_STRATEGY_TYPES:
            raise ValueError(f"策略类型无效: {value}")
        if field_name == "params":
            value = json.dumps(value) if isinstance(value, dict) else value
            updates.append("params = %s::jsonb")
        else:
            updates.append(f"{field_name} = %s")
        values.append(value)

    if not updates:
        return strategy

    updates.append("updated_at = NOW()")
    values.extend([strategy_id, user_id])

    try:
        sql = f"""
            UPDATE qd_strategies
            SET {', '.join(updates)}
            WHERE id = %s AND user_id = %s
            RETURNING *
        """
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(values))
            row = cursor.fetchone()
            conn.commit()
            if row:
                cols = [desc[0] for desc in cursor.description]
                return dict(zip(cols, row))
            return None
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"更新策略失败: {e}")
        return None


def delete_strategy(strategy_id: int, user_id: int) -> bool:
    """
    删除策略

    running 状态的策略不可删除。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID（只能删除自己的策略）

    Returns:
        是否删除成功
    """
    strategy = get_strategy(strategy_id, user_id)
    if not strategy:
        raise ValueError("策略不存在或无权访问")

    if strategy.get("status") == "running":
        raise ValueError("运行中的策略不能删除，请先停止")

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM qd_strategies WHERE id = %s AND user_id = %s",
                (strategy_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"删除策略失败: {e}")
        return False


def update_strategy_status(strategy_id: int, status: str) -> bool:
    """
    更新策略状态（状态机验证）

    状态转换规则:
    - draft → ready → running → stopped → draft（循环）
    - error 可从任何状态转入

    Args:
        strategy_id: 策略 ID
        status: 目标状态

    Returns:
        是否更新成功
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"无效状态: {status}")

    # 获取当前状态
    current = fetch_one(
        "SELECT status FROM qd_strategies WHERE id = %s",
        (strategy_id,),
    )
    if not current:
        raise ValueError("策略不存在")

    current_status = current["status"]
    allowed = STATUS_TRANSITIONS.get(current_status, set())

    if status not in allowed:
        raise ValueError(
            f"不允许从 '{current_status}' 转换到 '{status}'，"
            f"允许的目标状态: {', '.join(sorted(allowed)) or '无'}"
        )

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE qd_strategies SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, strategy_id),
            )
            conn.commit()
            ok = cursor.rowcount > 0
            if ok:
                logger.info(f"策略 {strategy_id} 状态更新: {current_status} → {status}")
            return ok
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"更新策略状态失败: {e}")
        return False


def get_running_strategies() -> List[Dict[str, Any]]:
    """
    获取所有运行中的策略

    Returns:
        运行中策略的列表（含基本信息）
    """
    try:
        return fetch_all(
            """
            SELECT id, user_id, name, market, symbol, timeframe, strategy_type
            FROM qd_strategies
            WHERE status = 'running'
            ORDER BY id
            """
        )
    except Exception as e:
        logger.error(f"获取运行中策略失败: {e}")
        return []
