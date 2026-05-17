# -*- coding: utf-8 -*-
"""
监控逻辑模块
实现网页内容抓取、变化检测和邮件通知
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime
from sqlalchemy.orm import Session
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Tuple
from database import MonitorTask, MonitorLog
import hashlib
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MonitorCore:
    """监控核心类"""

    def __init__(self, smtp_host: str = None, smtp_port: int = 587,
                 smtp_user: str = None, smtp_password: str = None,
                 sender_email: str = None):
        """初始化监控器

        Args:
            smtp_host: SMTP服务器地址
            smtp_port: SMTP端口
            smtp_user: SMTP用户名
            smtp_password: SMTP密码
            sender_email: 发件人邮箱
        """
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.sender_email = sender_email or smtp_user

        # 请求头，模拟浏览器访问
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }

    def fetch_content(self, url: str, css_selector: str) -> Tuple[Optional[str], Optional[str]]:
        """抓取网页指定元素的内容

        Args:
            url: 网页URL
            css_selector: CSS选择器

        Returns:
            (内容, 错误信息)
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'

            soup = BeautifulSoup(response.text, 'html.parser')
            elements = soup.select(css_selector)

            if not elements:
                return None, f"CSS选择器 '{css_selector}' 未找到匹配元素"

            # 如果有多个元素，返回第一个
            element = elements[0]

            # 获取文本内容
            content = element.get_text(strip=True)

            # 如果文本为空，尝试获取HTML
            if not content:
                content = str(element)

            return content, None

        except requests.exceptions.Timeout:
            return None, "请求超时"
        except requests.exceptions.ConnectionError:
            return None, "连接失败，请检查网络"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP错误: {e}"
        except Exception as e:
            return None, f"抓取失败: {str(e)}"

    def detect_change(self, old_content: Optional[str], new_content: str) -> Tuple[bool, Optional[str]]:
        """检测内容变化

        Args:
            old_content: 旧内容
            new_content: 新内容

        Returns:
            (是否变化, 变化类型)
        """
        if old_content is None:
            return True, "new"

        old_stripped = old_content.strip() if old_content else ""
        new_stripped = new_content.strip() if new_content else ""

        if not old_stripped and new_stripped:
            return True, "new"
        elif old_stripped and not new_stripped:
            return True, "removed"
        elif old_stripped != new_stripped:
            return True, "modified"

        return False, None

    def hash_content(self, content: str) -> str:
        """计算内容哈希值"""
        return hashlib.md5(content.encode('utf-8')).hexdigest()

    def send_email(self, to_email: str, subject: str, html_content: str) -> Tuple[bool, Optional[str]]:
        """发送邮件通知

        Args:
            to_email: 收件人邮箱
            subject: 邮件主题
            html_content: HTML邮件内容

        Returns:
            (是否成功, 错误信息)
        """
        if not self.smtp_host or not self.smtp_user or not self.smtp_password:
            logger.warning("SMTP配置不完整，跳过邮件发送")
            return False, "SMTP配置不完整"

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = to_email

            # 添加HTML内容
            html_part = MIMEText(html_content, 'html', 'utf-8')
            msg.attach(html_part)

            # 发送邮件
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            logger.info(f"邮件发送成功: {to_email}")
            return True, None

        except smtplib.SMTPAuthenticationError:
            return False, "SMTP认证失败，请检查用户名和密码"
        except smtplib.SMTPException as e:
            return False, f"SMTP错误: {str(e)}"
        except Exception as e:
            return False, f"邮件发送失败: {str(e)}"

    def create_notification_email(self, task: dict, old_content: Optional[str], new_content: str,
                                  change_type: str) -> Tuple[str, str]:
        """创建通知邮件内容

        Args:
            task: 任务信息字典
            old_content: 旧内容
            new_content: 新内容
            change_type: 变化类型

        Returns:
            (主题, HTML内容)
        """
        task_name = task.get('name', '未知任务')
        url = task.get('url', '')
        css_selector = task.get('css_selector', '')

        change_type_cn = {
            'new': '新增内容',
            'removed': '内容消失',
            'modified': '内容变化'
        }.get(change_type, change_type)

        subject = f"【监控告警】{task_name} - 检测到{change_type_cn}"

        # 截取内容预览
        old_preview = old_content[:200] + '...' if old_content and len(old_content) > 200 else (old_content or '(空)')
        new_preview = new_content[:200] + '...' if new_content and len(new_content) > 200 else new_content

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; }}
                .alert-box {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 20px; margin: 10px 0; }}
                .alert-title {{ color: #856404; font-size: 18px; font-weight: bold; margin-bottom: 15px; }}
                .info-row {{ margin: 10px 0; color: #333; }}
                .label {{ font-weight: bold; color: #555; }}
                .old-content {{ background: #f8f9fa; padding: 10px; border-radius: 4px; margin: 5px 0; }}
                .new-content {{ background: #d4edda; padding: 10px; border-radius: 4px; margin: 5px 0; }}
                .footer {{ margin-top: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="alert-box">
                <div class="alert-title">🔔 检测到网页内容变化</div>
                <div class="info-row"><span class="label">任务名称：</span>{task_name}</div>
                <div class="info-row"><span class="label">监控URL：</span><a href="{url}">{url}</a></div>
                <div class="info-row"><span class="label">CSS选择器：</span><code>{css_selector}</code></div>
                <div class="info-row"><span class="label">变化类型：</span>{change_type_cn}</div>
                <div class="info-row"><span class="label">检测时间：</span>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>

            <div style="margin: 20px 0;">
                <h4 style="color: #dc3545;">旧内容：</h4>
                <div class="old-content">{old_preview}</div>
            </div>

            <div style="margin: 20px 0;">
                <h4 style="color: #28a745;">新内容：</h4>
                <div class="new-content">{new_preview}</div>
            </div>

            <div class="footer">
                <p>此邮件由网页监控告警系统自动发送</p>
                <p>请勿直接回复此邮件</p>
            </div>
        </body>
        </html>
        """

        return subject, html_content


# 全局监控器实例（将在main.py中初始化）
monitor_instance: Optional[MonitorCore] = None


def get_monitor() -> MonitorCore:
    """获取监控器实例"""
    return monitor_instance


def set_monitor(monitor: MonitorCore):
    """设置监控器实例"""
    global monitor_instance
    monitor_instance = monitor


def check_task(db: Session, task: MonitorTask) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """执行单个监控任务

    Args:
        db: 数据库会话
        task: 监控任务对象

    Returns:
        (是否变化, 变化类型, 新内容, 错误信息)
    """
    monitor = get_monitor()
    if not monitor:
        return False, None, None, "监控器未初始化"

    # 抓取内容
    new_content, error = monitor.fetch_content(task.url, task.css_selector)
    if error:
        return False, None, None, error

    # 检测变化
    has_changed, change_type = monitor.detect_change(task.last_content, new_content)

    # 更新任务状态
    task.last_content = new_content
    task.last_check_time = datetime.now()
    task.status = "running"

    if has_changed:
        task.last_change_time = datetime.now()

    db.commit()

    return has_changed, change_type, new_content, error


def execute_task_with_notification(db: Session, task: MonitorTask) -> Tuple[bool, Optional[str]]:
    """执行任务并发送通知

    Args:
        db: 数据库会话
        task: 监控任务对象

    Returns:
        (是否成功, 错误信息)
    """
    monitor = get_monitor()
    if not monitor:
        return False, "监控器未初始化"

    try:
        # 执行检查
        has_changed, change_type, new_content, error = check_task(db, task)

        # 创建日志记录
        log = MonitorLog(
            task_id=task.id,
            check_time=datetime.now(),
            old_content=task.last_content,
            new_content=new_content or "",
            has_changed=has_changed,
            change_type=change_type
        )

        if error:
            log.error_message = error
            task.status = "error"
        else:
            task.status = "running"

        db.add(log)
        db.commit()

        # 如果有变化且配置了通知邮箱，发送邮件
        if has_changed and task.notify_email and monitor.smtp_host:
            subject, html_content = monitor.create_notification_email(
                task={
                    'name': task.name,
                    'url': task.url,
                    'css_selector': task.css_selector
                },
                old_content=task.last_content,
                new_content=new_content or "",
                change_type=change_type or "modified"
            )

            success, email_error = monitor.send_email(
                to_email=task.notify_email,
                subject=subject,
                html_content=html_content
            )

            if success:
                log.notify_sent = True
                db.commit()
            else:
                logger.warning(f"邮件发送失败: {email_error}")

        return True, None

    except Exception as e:
        logger.error(f"执行任务失败: {str(e)}")
        task.status = "error"
        db.commit()
        return False, str(e)