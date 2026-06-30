"""
QuantX CTP 期货交易适配器

基于 openctp-ctp 库实现 CTP 接口，支持：
- SimNow 模拟环境（默认）
- 实盘交易（需配置生产环境前置地址）

注意：CTP 集成较为复杂，当前版本提供框架接口。
实际连接和交易功能后续迭代完善。

参考文档：
- SimNow 模拟环境地址：180.168.146.187:10130
- openctp-ctp PyPI：https://pypi.org/project/openctp-ctp/
"""
from __future__ import annotations

import os
import time
import threading
import uuid
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------
# 懒加载 openctp（未安装时优雅降级）
# ------------------------------------------------------------------

_has_openctp = False
_openctp_error = ""

try:
    from openctp_ctp import tdapi, mdapi
    _has_openctp = True
except ImportError as e:
    _openctp_error = str(e)
    logger.info(
        "openctp-ctp 未安装，CTP 适配器将以框架模式运行。"
        "如需实际连接，请运行: pip install openctp-ctp"
    )


# SimNow 模拟环境服务器地址
SIMNOW_TD_SERVER = "180.168.146.187:10130"
SIMNOW_MD_SERVER = "180.168.146.187:10131"


def _gen_order_ref() -> str:
    """生成订单引用"""
    return f"CTP-{uuid.uuid4().hex[:10].upper()}"


class CTPAdapter:
    """CTP 期货交易适配器

    封装 openctp-ctp 库，提供与 PaperAdapter / AlpacaAdapter 统一的接口。

    用法::

        adapter = CTPAdapter(paper=True)
        adapter.connect()
        result = adapter.submit_order("rb2405", "buy", 1)
        adapter.disconnect()
    """

    adapter_type = "ctp"

    def __init__(
        self,
        broker_id: str = "9999",
        paper: bool = True,
    ):
        """
        初始化 CTP 适配器

        Args:
            broker_id: 期货公司代码（SimNow 默认为 9999）
            paper: True 使用 SimNow 模拟环境，False 使用实盘
        """
        self.broker_id = broker_id
        self.paper = paper
        self._connected = False
        self._td_api = None
        self._login_required = False

        # 从环境变量读取 SimNow 账户
        self._user_id = os.getenv("CTP_USER_ID", "").strip()
        self._password = os.getenv("CTP_PASSWORD", "").strip()
        self._auth_code = os.getenv("CTP_AUTH_CODE", "0000000000000000").strip()
        self._app_id = os.getenv("CTP_APP_ID", "simnow_client_test").strip()

        # SimNow 环境地址（可覆盖）
        self._td_server = os.getenv("CTP_TD_SERVER", SIMNOW_TD_SERVER).strip()
        self._md_server = os.getenv("CTP_MD_SERVER", SIMNOW_MD_SERVER).strip()

        # 内存状态
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._orders: List[Dict[str, Any]] = []
        self._account_info: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接到 CTP 前置

        Returns:
            是否连接成功
        """
        if not _has_openctp:
            logger.warning(
                f"CTP 框架模式: openctp-ctp 未安装 ({_openctp_error})。"
                f"当前以模拟方式运行。"
            )
            self._connected = True
            return True

        try:
            # 创建 TdApi 实例并注册前置地址
            self._td_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi("")
            self._td_api.RegisterFront(self._td_server)
            self._td_api.Init()

            # 等待连接（简化：最多等 5 秒）
            for _ in range(50):
                if self._connected:
                    break
                time.sleep(0.1)

            if self._connected:
                logger.info(f"CTP 连接成功: broker={self.broker_id}, server={self._td_server}")
            else:
                logger.warning("CTP 连接超时，将使用框架模式")
                self._connected = True  # 框架模式

            return True

        except Exception as e:
            logger.error(f"CTP 连接失败: {e}")
            self._connected = True  # 降级为框架模式
            return True

    def disconnect(self) -> None:
        """断开 CTP 连接"""
        if self._td_api and _has_openctp:
            try:
                self._td_api.Release()
            except Exception as e:
                logger.warning(f"CTP 断开连接时出错: {e}")
        self._connected = False
        self._td_api = None
        logger.info("CTP 已断开连接")

    @property
    def connected(self) -> bool:
        """是否已连接"""
        return self._connected

    # ------------------------------------------------------------------
    # 账户与持仓
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """获取账户资金信息

        Returns:
            {
                "user_id": str,
                "broker_id": str,
                "balance": float,
                "available": float,
                "margin": float,
                "frozen_margin": float,
                "paper": bool,
            }
        """
        if not _has_openctp or not self._td_api:
            # 框架模式：返回模拟数据
            return {
                "user_id": self._user_id or "FRAMEWORK",
                "broker_id": self.broker_id,
                "balance": 0.0,
                "available": 0.0,
                "margin": 0.0,
                "frozen_margin": 0.0,
                "paper": self.paper,
                "adapter_type": self.adapter_type,
                "framework_mode": True,
            }

        # TODO: 实际 CTP 查询需要异步回调处理
        return self._account_info or {
            "user_id": self._user_id,
            "broker_id": self.broker_id,
            "balance": 0.0,
            "available": 0.0,
            "margin": 0.0,
            "frozen_margin": 0.0,
            "paper": self.paper,
            "adapter_type": self.adapter_type,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """获取持仓列表

        Returns:
            [{"instrument_id", "direction", "volume", "open_price", ...}, ...]
        """
        if not _has_openctp or not self._td_api:
            # 框架模式
            with self._lock:
                return list(self._positions.values())

        # TODO: 实际 CTP 查询需要异步回调处理
        with self._lock:
            return list(self._positions.values())

    def get_portfolio_value(self) -> float:
        """获取组合总值"""
        account = self.get_account()
        return account.get("balance", 0.0)

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(
        self,
        instrument_id: str,
        direction: str,
        volume: int,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        提交期货订单

        Args:
            instrument_id: 合约代码（如 "rb2405"）
            direction: "buy"（开多）或 "sell"（开空）
            volume: 手数
            price: 限价（None 为市价/对手价）

        Returns:
            {"order_ref": str, "status": str, "reason": str, ...}
        """
        order_ref = _gen_order_ref()
        direction = (direction or "").strip().lower()

        if not instrument_id:
            return {
                "order_ref": order_ref,
                "status": "rejected",
                "reason": "合约代码不能为空",
            }

        if direction not in ("buy", "sell"):
            return {
                "order_ref": order_ref,
                "status": "rejected",
                "reason": f"无效方向: {direction}，需要 buy 或 sell",
            }

        if volume <= 0:
            return {
                "order_ref": order_ref,
                "status": "rejected",
                "reason": "手数必须大于 0",
            }

        if not _has_openctp or not self._td_api:
            # 框架模式：模拟订单
            logger.info(
                f"CTP 框架模式订单: {direction} {instrument_id} x{volume} "
                f"@{price or 'market'} (ref={order_ref})"
            )
            with self._lock:
                order = {
                    "order_ref": order_ref,
                    "instrument_id": instrument_id,
                    "direction": direction,
                    "volume": volume,
                    "price": price or 0.0,
                    "status": "simulated",
                    "created_at": time.time(),
                }
                self._orders.append(order)

            return {
                "order_ref": order_ref,
                "status": "simulated",
                "reason": "框架模式（openctp 未安装或未连接）",
                "instrument_id": instrument_id,
                "direction": direction,
                "volume": volume,
            }

        # TODO: 实际 CTP 下单逻辑
        # 需要构造 CThostFtdcInputOrderField 并通过 ReqOrderInsert 发送
        try:
            order_field = tdapi.CThostFtdcInputOrderField()
            order_field.BrokerID = self.broker_id
            order_field.InvestorID = self._user_id
            order_field.InstrumentID = instrument_id
            order_field.OrderRef = order_ref[:13]  # CTP 限制 13 字符

            # 方向映射
            if direction == "buy":
                order_field.Direction = tdapi.THOST_FTDC_D_Buy
            else:
                order_field.Direction = tdapi.THOST_FTDC_D_Sell

            # 开平标志：默认开仓
            order_field.CombOffsetFlag = tdapi.THOST_FTDC_OF_Open
            order_field.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation

            # 价格
            if price and price > 0:
                order_field.LimitPrice = price
                order_field.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
            else:
                order_field.OrderPriceType = tdapi.THOST_FTDC_OPT_AnyPrice

            order_field.VolumeTotalOriginal = volume
            order_field.TimeCondition = tdapi.THOST_FTDC_TC_IOC  # 立即完成
            order_field.VolumeCondition = tdapi.THOST_FTDC_VC_AV  # 任意数量
            order_field.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
            order_field.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose

            ret = self._td_api.ReqOrderInsert(order_field, 0)
            if ret == 0:
                logger.info(f"CTP 订单已提交: {direction} {instrument_id} x{volume}")
                return {
                    "order_ref": order_ref,
                    "status": "submitted",
                    "reason": "",
                }
            else:
                return {
                    "order_ref": order_ref,
                    "status": "rejected",
                    "reason": f"CTP 返回错误码: {ret}",
                }

        except Exception as e:
            logger.error(f"CTP 订单提交失败: {e}")
            return {
                "order_ref": order_ref,
                "status": "rejected",
                "reason": str(e),
            }

    def cancel_order(self, order_ref: str) -> bool:
        """撤单

        Args:
            order_ref: 订单引用

        Returns:
            是否撤单成功
        """
        if not _has_openctp or not self._td_api:
            logger.info(f"CTP 框架模式撤单: {order_ref}")
            return True

        # TODO: 实际 CTP 撤单逻辑
        try:
            action_field = tdapi.CThostFtdcInputOrderActionField()
            action_field.BrokerID = self.broker_id
            action_field.InvestorID = self._user_id
            action_field.OrderRef = order_ref[:13]
            action_field.ActionFlag = tdapi.THOST_FTDC_AF_Delete

            ret = self._td_api.ReqOrderAction(action_field, 0)
            return ret == 0
        except Exception as e:
            logger.error(f"CTP 撤单失败 ({order_ref}): {e}")
            return False

    def get_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取订单历史

        Args:
            limit: 返回数量上限

        Returns:
            订单列表
        """
        with self._lock:
            return list(reversed(self._orders[-limit:]))
