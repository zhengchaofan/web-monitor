# -*- coding: utf-8 -*-
"""
数据库模型模块
定义 MonitorTask 任务表和 MonitorLog 日志表
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

Base = declarative_base()


class MonitorTask(Base):
    """监控任务表"""
    __tablename__ = "monitor_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, comment="任务名称")
    url = Column(String(500), nullable=False, comment="监控网页URL")
    css_selector = Column(String(200), nullable=False, comment="CSS选择器")
    check_interval = Column(Integer, default=5, comment="检查间隔(分钟)")
    last_content = Column(Text, nullable=True, comment="上次抓取的内容")
    last_check_time = Column(DateTime, nullable=True, comment="上次检查时间")
    last_change_time = Column(DateTime, nullable=True, comment="上次变化时间")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
    notify_email = Column(String(200), nullable=True, comment="通知邮箱")
    status = Column(String(50), default="pending", comment="状态: pending/running/error/stopped")

    def __repr__(self):
        return f"<MonitorTask(id={self.id}, name={self.name}, url={self.url})>"


class MonitorLog(Base):
    """监控日志表"""
    __tablename__ = "monitor_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, nullable=False, comment="关联任务ID")
    check_time = Column(DateTime, default=datetime.now, comment="检查时间")
    old_content = Column(Text, nullable=True, comment="变化前内容")
    new_content = Column(Text, nullable=False, comment="变化后内容")
    has_changed = Column(Boolean, default=False, comment="是否发生变化")
    change_type = Column(String(50), nullable=True, comment="变化类型: new/removed/modified")
    error_message = Column(Text, nullable=True, comment="错误信息")
    notify_sent = Column(Boolean, default=False, comment="是否已发送通知")

    def __repr__(self):
        return f"<MonitorLog(id={self.id}, task_id={self.task_id}, has_changed={self.has_changed})>"


import os

# 数据库连接管理
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///web_monitor.db")
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """初始化数据库，创建所有表"""
    Base.metadata.create_all(bind=engine)


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()