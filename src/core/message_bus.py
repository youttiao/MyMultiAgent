"""
Message Bus

简化自 ruflo (https://github.com/ruvnet/claude-flow)
轻量级事件驱动消息总线，用于 Agent 间通信。

核心功能:
- 发布/订阅模式
- 事件驱动通知
- Agent 间消息传递
"""

import threading
import time
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Any, Optional
from collections import defaultdict
import queue


class MessageType(Enum):
    """消息类型"""
    TASK = "task"
    RESULT = "result"
    EVENT = "event"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    CONTROL = "control"


@dataclass
class Message:
    """消息结构"""
    id: str
    msg_type: MessageType
    sender: str
    recipient: Optional[str]  # None 表示广播
    content: Any
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.msg_type.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
        }


class MessageBus:
    """
    消息总线

    提供 Agent 间消息传递的发布/订阅机制

    使用示例:
        bus = MessageBus()

        # 订阅消息
        def handle_task(msg: Message):
            print(f"收到任务: {msg.content}")

        bus.subscribe("agent-1", MessageType.TASK, handle_task)

        # 发布消息
        bus.publish(Message(
            msg_type=MessageType.TASK,
            sender="agent-0",
            recipient="agent-1",
            content={"task": "do something"}
        ))
    """

    def __init__(self):
        # 订阅者: {msg_type: {recipient: [handler, ...]}}
        self._subscriptions: dict[MessageType, dict[str, list[Callable]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # 全局订阅者（接收所有消息）
        self._global_subscribers: dict[str, Callable] = {}

        # 消息队列
        self._queue: queue.Queue = queue.Queue()
        self._pending: list[Message] = []

        # 线程安全
        self._lock = threading.RLock()

        # 消息处理器线程
        self._running = False
        self._processor_thread: Optional[threading.Thread] = None

        # 统计
        self._stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "messages_broadcast": 0,
        }

    def start(self) -> None:
        """启动消息处理器"""
        if self._running:
            return

        self._running = True
        self._processor_thread = threading.Thread(target=self._process_messages, daemon=True)
        self._processor_thread.start()

    def stop(self) -> None:
        """停止消息处理器"""
        self._running = False
        if self._processor_thread:
            self._processor_thread.join(timeout=1.0)

    def _process_messages(self) -> None:
        """消息处理循环"""
        while self._running:
            try:
                # 从队列获取消息，超时 0.1 秒
                try:
                    msg = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                self._deliver_message(msg)

            except Exception as e:
                print(f"Error processing message: {e}")

    def _deliver_message(self, msg: Message) -> None:
        """投递消息到订阅者"""
        with self._lock:
            # 更新统计
            if msg.recipient:
                self._stats["messages_sent"] += 1
            else:
                self._stats["messages_broadcast"] += 1
            self._stats["messages_received"] += 1

            # 投递给全局订阅者
            for subscriber in self._global_subscribers.values():
                try:
                    subscriber(msg)
                except Exception as e:
                    print(f"Error in global subscriber: {e}")

            # 投递给特定订阅者
            handlers = self._subscriptions.get(msg.msg_type, {}).get(msg.recipient or "", [])
            for handler in handlers:
                try:
                    handler(msg)
                except Exception as e:
                    print(f"Error delivering message: {e}")

            # 广播给所有订阅者（如果指定了接收者）
            if msg.recipient:
                broadcast_handlers = self._subscriptions.get(msg.msg_type, {}).get("*", [])
                for handler in broadcast_handlers:
                    try:
                        handler(msg)
                    except Exception as e:
                        print(f"Error in broadcast handler: {e}")

    def subscribe(
        self,
        recipient: str,
        msg_type: MessageType,
        handler: Callable[[Message], None]
    ) -> str:
        """
        订阅消息

        Args:
            recipient: 接收者标识
            msg_type: 消息类型
            handler: 处理函数

        Returns:
            str: 订阅 ID
        """
        with self._lock:
            subscription_id = str(uuid.uuid4())
            self._subscriptions[msg_type][recipient].append(handler)
            return subscription_id

    def subscribe_global(
        self,
        subscriber_id: str,
        handler: Callable[[Message], None]
    ) -> None:
        """
        全局订阅

        订阅者会接收所有消息

        Args:
            subscriber_id: 订阅者 ID
            handler: 处理函数
        """
        with self._lock:
            self._global_subscribers[subscriber_id] = handler

    def unsubscribe(self, recipient: str, msg_type: MessageType, subscription_id: str = None) -> None:
        """
        取消订阅

        Args:
            recipient: 接收者标识
            msg_type: 消息类型
            subscription_id: 订阅 ID（None 则取消所有）
        """
        with self._lock:
            if subscription_id:
                # 简化实现：暂不支持按 ID 取消
                pass
            else:
                self._subscriptions[msg_type][recipient].clear()

    def unsubscribe_global(self, subscriber_id: str) -> None:
        """取消全局订阅"""
        with self._lock:
            self._global_subscribers.pop(subscriber_id, None)

    def publish(self, msg: Message) -> None:
        """
        发布消息

        Args:
            msg: 消息对象
        """
        if not msg.id:
            msg.id = str(uuid.uuid4())

        self._queue.put(msg)

    def send_to(
        self,
        sender: str,
        recipient: str,
        msg_type: MessageType,
        content: Any,
        correlation_id: Optional[str] = None
    ) -> Message:
        """
        发送消息到指定接收者

        Args:
            sender: 发送者
            recipient: 接收者
            msg_type: 消息类型
            content: 消息内容
            correlation_id: 关联 ID（用于响应追踪）

        Returns:
            Message: 创建的消息对象
        """
        msg = Message(
            id=str(uuid.uuid4()),
            msg_type=msg_type,
            sender=sender,
            recipient=recipient,
            content=content,
            correlation_id=correlation_id
        )
        self.publish(msg)
        return msg

    def broadcast(
        self,
        sender: str,
        msg_type: MessageType,
        content: Any
    ) -> Message:
        """
        广播消息

        Args:
            sender: 发送者
            msg_type: 消息类型
            content: 消息内容

        Returns:
            Message: 创建的消息对象
        """
        msg = Message(
            id=str(uuid.uuid4()),
            msg_type=msg_type,
            sender=sender,
            recipient=None,  # 广播
            content=content
        )
        self.publish(msg)
        return msg

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._lock:
            return dict(self._stats)

    def get_pending_count(self) -> int:
        """获取待处理消息数"""
        return self._queue.qsize()


# 全局消息总线实例
_global_bus: Optional[MessageBus] = None


def get_message_bus() -> MessageBus:
    """获取全局消息总线"""
    global _global_bus
    if _global_bus is None:
        _global_bus = MessageBus()
        _global_bus.start()
    return _global_bus


def shutdown_message_bus() -> None:
    """关闭全局消息总线"""
    global _global_bus
    if _global_bus:
        _global_bus.stop()
        _global_bus = None
