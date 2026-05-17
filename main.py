# -*- coding: utf-8 -*-
"""
网页监控告警系统 - FastAPI主程序
包含Web管理界面和任务调度
"""

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import uvicorn
import os

from database import Base, engine, get_db, init_db, MonitorTask, MonitorLog
from monitor import MonitorCore, set_monitor, execute_task_with_notification, get_monitor

# 创建数据库表
init_db()

# 创建FastAPI应用
app = FastAPI(
    title="网页监控告警系统",
    description="监控网页内容变化并发送邮件通知",
    version="1.0.0"
)

# 配置
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")

# 初始化监控器
monitor = MonitorCore(
    smtp_host=SMTP_HOST,
    smtp_port=SMTP_PORT,
    smtp_user=SMTP_USER,
    smtp_password=SMTP_PASSWORD,
    sender_email=SENDER_EMAIL
)
set_monitor(monitor)

# 定时任务调度器
scheduler = BackgroundScheduler()


# ==================== Pydantic模型 ====================

class TaskCreate(BaseModel):
    name: str
    url: str
    css_selector: str
    check_interval: int = 5
    notify_email: Optional[str] = None
    is_active: bool = True


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    css_selector: Optional[str] = None
    check_interval: Optional[int] = None
    notify_email: Optional[str] = None
    is_active: Optional[bool] = None


class TaskResponse(BaseModel):
    id: int
    name: str
    url: str
    css_selector: str
    check_interval: int
    last_content: Optional[str]
    last_check_time: Optional[datetime]
    last_change_time: Optional[datetime]
    is_active: bool
    created_at: datetime
    notify_email: Optional[str]
    status: str

    class Config:
        from_attributes = True


# ==================== 辅助函数 ====================

def get_scheduler():
    """获取调度器实例"""
    return scheduler


def run_scheduled_task(task_id: int):
    """执行定时任务"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
        if task and task.is_active:
            execute_task_with_notification(db, task)
    finally:
        db.close()


# ==================== API路由 ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    """主页"""
    return get_html_content()


@app.get("/api/tasks", response_model=List[TaskResponse])
async def list_tasks(db: Session = Depends(get_db)):
    """获取所有监控任务"""
    tasks = db.query(MonitorTask).order_by(MonitorTask.created_at.desc()).all()
    return tasks


@app.post("/api/tasks")
async def create_task(task_data: TaskCreate, db: Session = Depends(get_db)):
    """创建新的监控任务"""
    task = MonitorTask(
        name=task_data.name,
        url=task_data.url,
        css_selector=task_data.css_selector,
        check_interval=task_data.check_interval,
        notify_email=task_data.notify_email,
        is_active=task_data.is_active,
        status="pending"
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 如果任务启用，添加到调度器
    if task.is_active:
        add_job_to_scheduler(task)

    return task


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, task_data: TaskUpdate, db: Session = Depends(get_db)):
    """更新监控任务"""
    task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    update_data = task_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(task, key, value)

    db.commit()
    db.refresh(task)

    # 更新调度器
    remove_job_from_scheduler(task_id)
    if task.is_active:
        add_job_to_scheduler(task)

    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, db: Session = Depends(get_db)):
    """删除监控任务"""
    task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 从调度器移除
    remove_job_from_scheduler(task_id)

    db.delete(task)
    db.commit()

    return {"message": "任务已删除"}


@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, db: Session = Depends(get_db)):
    """启用/禁用任务"""
    task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.is_active = not task.is_active
    task.status = "pending" if task.is_active else "stopped"
    db.commit()

    # 更新调度器
    remove_job_from_scheduler(task_id)
    if task.is_active:
        add_job_to_scheduler(task)

    return task


@app.post("/api/tasks/{task_id}/check")
async def check_task_now(task_id: int, db: Session = Depends(get_db)):
    """立即执行任务检查"""
    task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    success, error = execute_task_with_notification(db, task)

    if not success:
        raise HTTPException(status_code=500, detail=error)

    return {
        "message": "检查完成",
        "has_changed": task.last_change_time is not None,
        "last_content": task.last_content
    }


@app.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int, limit: int = 50, db: Session = Depends(get_db)):
    """获取任务日志"""
    task = db.query(MonitorTask).filter(MonitorTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    logs = db.query(MonitorLog).filter(
        MonitorLog.task_id == task_id
    ).order_by(MonitorLog.check_time.desc()).limit(limit).all()

    return [
        {
            "id": log.id,
            "check_time": log.check_time,
            "has_changed": log.has_changed,
            "change_type": log.change_type,
            "old_content": log.old_content,
            "new_content": log.new_content,
            "error_message": log.error_message,
            "notify_sent": log.notify_sent
        }
        for log in logs
    ]


@app.get("/api/config")
async def get_config():
    """获取SMTP配置状态"""
    return {
        "smtp_configured": bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD),
        "smtp_host": SMTP_HOST if SMTP_HOST else None,
        "smtp_port": SMTP_PORT
    }


# ==================== 调度器管理 ====================

def add_job_to_scheduler(task: MonitorTask):
    """添加任务到调度器"""
    job_id = f"task_{task.id}"
    try:
        # 移除已存在的任务
        remove_job_from_scheduler(task.id)

        scheduler.add_job(
            func=run_scheduled_task,
            trigger=IntervalTrigger(minutes=task.check_interval),
            args=[task.id],
            id=job_id,
            name=f"监控任务: {task.name}",
            replace_existing=True
        )
    except Exception as e:
        print(f"添加调度任务失败: {e}")


def remove_job_from_scheduler(task_id: int):
    """从调度器移除任务"""
    job_id = f"task_{task_id}"
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    except Exception:
        pass


def init_scheduler():
    """初始化调度器，加载所有启用的任务"""
    db = next(get_db())
    try:
        tasks = db.query(MonitorTask).filter(MonitorTask.is_active == True).all()
        for task in tasks:
            add_job_to_scheduler(task)
        scheduler.start()
    finally:
        db.close()


# ==================== HTML页面 ====================

def get_html_content():
    """生成HTML管理界面"""
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>网页监控告警系统</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .header {
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .header h1 { color: #333; font-size: 24px; margin-bottom: 8px; }
        .header p { color: #666; font-size: 14px; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .card h2 {
            color: #333;
            font-size: 18px;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 2px solid #667eea;
        }
        .btn {
            display: inline-block;
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.3s;
        }
        .btn-primary {
            background: #667eea;
            color: white;
        }
        .btn-primary:hover { background: #5568d3; }
        .btn-success { background: #28a745; color: white; }
        .btn-success:hover { background: #218838; }
        .btn-danger { background: #dc3545; color: white; }
        .btn-danger:hover { background: #c82333; }
        .btn-warning { background: #ffc107; color: #333; }
        .btn-warning:hover { background: #e0a800; }
        .btn-sm { padding: 6px 12px; font-size: 12px; }
        .form-group { margin-bottom: 16px; }
        .form-group label {
            display: block;
            margin-bottom: 6px;
            color: #333;
            font-weight: 500;
        }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
        }
        .form-group input:focus, .form-group textarea:focus {
            outline: none;
            border-color: #667eea;
        }
        .form-group textarea { min-height: 80px; resize: vertical; }
        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .task-list { display: flex; flex-direction: column; gap: 12px; }
        .task-item {
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 16px;
            transition: all 0.3s;
        }
        .task-item:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .task-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .task-name { font-size: 16px; font-weight: 600; color: #333; }
        .task-status {
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
        }
        .status-active { background: #d4edda; color: #28a745; }
        .status-inactive { background: #f8f9fa; color: #6c757d; }
        .status-running { background: #cce5ff; color: #007bff; }
        .status-error { background: #f8d7da; color: #dc3545; }
        .task-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 8px;
            font-size: 13px;
            color: #666;
            margin-bottom: 12px;
        }
        .task-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 600px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h3 { color: #333; }
        .modal-close {
            background: none;
            border: none;
            font-size: 24px;
            cursor: pointer;
            color: #666;
        }
        .config-note {
            background: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }
        .config-note h4 { color: #856404; margin-bottom: 8px; }
        .config-note code {
            background: #fff;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 13px;
        }
        .log-list { max-height: 400px; overflow-y: auto; }
        .log-item {
            padding: 12px;
            border-bottom: 1px solid #eee;
        }
        .log-item:last-child { border-bottom: none; }
        .log-time { color: #666; font-size: 12px; }
        .log-changed { color: #28a745; font-weight: 600; }
        .log-unchanged { color: #6c757d; }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .empty-state svg { width: 80px; height: 80px; margin-bottom: 16px; opacity: 0.5; }
        .loading { text-align: center; padding: 20px; }
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 16px 24px;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            z-index: 2000;
            animation: slideIn 0.3s ease;
        }
        .toast-success { background: #28a745; }
        .toast-error { background: #dc3545; }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔔 网页监控告警系统</h1>
            <p>监控网页内容变化，发现变化时自动发送邮件通知</p>
        </div>

        <div class="config-note" id="configNote" style="display: none;">
            <h4>⚠️ SMTP未配置</h4>
            <p>邮件通知功能暂时不可用。请设置以下环境变量：</p>
            <p><code>SMTP_HOST</code>, <code>SMTP_PORT</code>, <code>SMTP_USER</code>, <code>SMTP_PASSWORD</code></p>
        </div>

        <div class="card">
            <div class="modal-header">
                <h2>📋 监控任务</h2>
                <button class="btn btn-primary" onclick="openModal()">+ 添加任务</button>
            </div>
            <div class="task-list" id="taskList">
                <div class="loading">加载中...</div>
            </div>
        </div>
    </div>

    <!-- 添加/编辑任务弹窗 -->
    <div class="modal" id="taskModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modalTitle">添加监控任务</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <form id="taskForm">
                <input type="hidden" id="taskId">
                <div class="form-group">
                    <label>任务名称 *</label>
                    <input type="text" id="taskName" required placeholder="例如：iPhone 15 价格监控">
                </div>
                <div class="form-group">
                    <label>监控网址 *</label>
                    <input type="url" id="taskUrl" required placeholder="https://example.com/product">
                </div>
                <div class="form-group">
                    <label>CSS选择器 *</label>
                    <input type="text" id="taskSelector" required placeholder=".price, #product-title, span[itemprop=price]">
                    <small style="color: #666; font-size: 12px; margin-top: 4px; display: block;">
                        如何获取选择器？<a href="#" onclick="showHelp(); return false;">查看帮助</a>
                    </small>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>检查间隔（分钟）</label>
                        <input type="number" id="taskInterval" value="5" min="1" max="1440">
                    </div>
                    <div class="form-group">
                        <label>通知邮箱</label>
<input type="email" id="taskEmail" placeholder="notification@example.com">
                    </div>
                </div>
                <div class="form-group" style="display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" id="taskActive" checked style="width: auto;">
                    <label for="taskActive" style="margin-bottom: 0;">启用监控</label>
                </div>
                <div style="display: flex; gap: 12px; justify-content: flex-end; margin-top: 20px;">
                    <button type="button" class="btn btn-danger" onclick="closeModal()">取消</button>
                    <button type="submit" class="btn btn-primary">保存</button>
                </div>
            </form>
        </div>
    </div>

    <!-- 日志查看弹窗 -->
    <div class="modal" id="logModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>监控日志</h3>
                <button class="modal-close" onclick="closeLogModal()">&times;</button>
            </div>
            <div class="log-list" id="logList"></div>
        </div>
    </div>

    <!-- 帮助弹窗 -->
    <div class="modal" id="helpModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>如何获取CSS选择器</h3>
                <button class="modal-close" onclick="closeHelpModal()">&times;</button>
            </div>
            <div style="line-height: 1.8; color: #333;">
                <h4 style="color: #667eea; margin-bottom: 12px;">方法一：使用浏览器开发者工具</h4>
                <ol style="margin-left: 20px; margin-bottom: 20px;">
                    <li>在目标网页上，按 <code>F12</code> 或右键选择「检查」打开开发者工具</li>
                    <li>点击开发者工具左上角的箭头图标（选择元素）</li>
                    <li>点击你想要监控的页面元素</li>
                    <li>在Elements面板中，右键点击该元素</li>
                    <li>选择 <code>Copy</code> → <code>Copy selector</code></li>
                </ol>

                <h4 style="color: #667eea; margin-bottom: 12px;">方法二：手动编写选择器</h4>
                <p style="margin-bottom: 12px;">常见选择器示例：</p>
                <ul style="margin-left: 20px;">
                    <li><code>#price</code> - ID选择器，监控id="price"的元素</li>
                    <li><code>.product-price</code> - class选择器</li>
                    <li><code>[itemprop=price]</code> - 属性选择器</li>
                    <li><code>span.price</code> - 标签+class组合</li>
                    <li><code>div[class*=price]</code> - class包含price的元素</li>
                </ul>

                <h4 style="color: #667eea; margin-bottom: 12px; margin-top: 20px;">提示</h4>
                <ul style="margin-left: 20px;">
                    <li>建议选择包含具体内容的元素，而非整个容器</li>
                    <li>如果选择器匹配多个元素，只监控第一个</li>
                    <li>选择器越精确，监控结果越准确</li>
                </ul>
            </div>
        </div>
    </div>

    <script>
        let currentEditId = null;

        // 加载任务列表
        async function loadTasks() {
            try {
                const response = await fetch('/api/tasks');
                const tasks = await response.json();
                const taskList = document.getElementById('taskList');

                if (tasks.length === 0) {
                    taskList.innerHTML = `
                        <div class="empty-state">
                            <svg viewBox="0 0 24 24" fill="currentColor">
                                <path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 14H7v-2h5v2zm5-4H7v-2h10v2zm0-4H7V7h10v2z"/>
                            </svg>
                            <p>暂无监控任务</p>
                            <p style="font-size: 13px;">点击上方「添加任务」按钮创建第一个监控</p>
                        </div>
                    `;
                    return;
                }

                taskList.innerHTML = tasks.map(task => `
                    <div class="task-item">
                        <div class="task-header">
                            <span class="task-name">${escapeHtml(task.name)}</span>
                            <span class="task-status ${getStatusClass(task)}">${getStatusText(task)}</span>
                        </div>
                        <div class="task-info">
                            <span>📌 URL: ${escapeHtml(task.url)}</span>
                            <span>🎯 选择器: <code>${escapeHtml(task.css_selector)}</code></span>
                            <span>⏱️ 间隔: ${task.check_interval}分钟</span>
                            <span>🕐 上次检查: ${task.last_check_time ? formatDate(task.last_check_time) : '未检查'}</span>
                        </div>
                        <div class="task-actions">
                            <button class="btn btn-primary btn-sm" onclick="checkNow(${task.id})">🔍 立即检查</button>
                            <button class="btn btn-warning btn-sm" onclick="viewLogs(${task.id})">📜 日志</button>
                            <button class="btn btn-success btn-sm" onclick="toggleTask(${task.id})">${task.is_active ? '⏸️ 暂停' : '▶️ 启用'}</button>
                            <button class="btn btn-primary btn-sm" onclick="editTask(${task.id}, ${JSON.stringify(task).replace(/"/g, '&quot;')})">✏️ 编辑</button>
                            <button class="btn btn-danger btn-sm" onclick="deleteTask(${task.id})">🗑️ 删除</button>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('加载任务失败:', error);
                document.getElementById('taskList').innerHTML = '<div class="empty-state">加载失败</div>';
            }
        }

        function getStatusClass(task) {
            if (!task.is_active) return 'status-inactive';
            if (task.status === 'error') return 'status-error';
            if (task.status === 'running') return 'status-running';
            return 'status-active';
        }

        function getStatusText(task) {
            if (!task.is_active) return '已暂停';
            if (task.status === 'error') return '错误';
            if (task.status === 'running') return '运行中';
            return '就绪';
        }

        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatDate(dateStr) {
            const date = new Date(dateStr);
            return date.toLocaleString('zh-CN', { hour12: false });
        }

        // 弹窗操作
        function openModal(taskId = null) {
            currentEditId = taskId;
            document.getElementById('taskModal').classList.add('active');
            document.getElementById('modalTitle').textContent = taskId ? '编辑监控任务' : '添加监控任务';
        }

        function closeModal() {
            document.getElementById('taskModal').classList.remove('active');
            document.getElementById('taskForm').reset();
            currentEditId = null;
        }

        function editTask(id, task) {
            currentEditId = id;
            document.getElementById('taskId').value = id;
            document.getElementById('taskName').value = task.name;
            document.getElementById('taskUrl').value = task.url;
            document.getElementById('taskSelector').value = task.css_selector;
            document.getElementById('taskInterval').value = task.check_interval;
            document.getElementById('taskEmail').value = task.notify_email || '';
            document.getElementById('taskActive').checked = task.is_active;
            document.getElementById('taskModal').classList.add('active');
        }

        function viewLogs(taskId) {
            document.getElementById('logModal').classList.add('active');
            loadLogs(taskId);
        }

        function closeLogModal() {
            document.getElementById('logModal').classList.remove('active');
        }

        async function loadLogs(taskId) {
            try {
                const response = await fetch(`/api/tasks/${taskId}/logs`);
                const logs = await response.json();
                const logList = document.getElementById('logList');

                if (logs.length === 0) {
                    logList.innerHTML = '<div class="empty-state">暂无日志</div>';
                    return;
                }

                logList.innerHTML = logs.map(log => `
                    <div class="log-item">
                        <div class="log-time">${formatDate(log.check_time)}</div>
                        <div class="${log.has_changed ? 'log-changed' : 'log-unchanged'}">
                            ${log.has_changed ? '✓ 发生变化 (' + log.change_type + ')' : '✗ 无变化'}
                            ${log.notify_sent ? ' | 📧 已通知' : ''}
                        </div>
                        ${log.error_message ? '<div style="color: #dc3545; font-size: 12px;">错误: ' + escapeHtml(log.error_message) + '</div>' : ''}
                    </div>
                `).join('');
            } catch (error) {
                console.error('加载日志失败:', error);
            }
        }

        function showHelp() {
            document.getElementById('helpModal').classList.add('active');
        }

        function closeHelpModal() {
            document.getElementById('helpModal').classList.remove('active');
        }

        // API操作
        document.getElementById('taskForm').addEventListener('submit', async (e) => {
            e.preventDefault();

            const taskData = {
                name: document.getElementById('taskName').value,
                url: document.getElementById('taskUrl').value,
                css_selector: document.getElementById('taskSelector').value,
                check_interval: parseInt(document.getElementById('taskInterval').value),
                notify_email: document.getElementById('taskEmail').value || null,
                is_active: document.getElementById('taskActive').checked
            };

            try {
                let response;
                if (currentEditId) {
                    response = await fetch(`/api/tasks/${currentEditId}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(taskData)
                    });
                } else {
                    response = await fetch('/api/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(taskData)
                    });
                }

                if (response.ok) {
                    showToast('保存成功', 'success');
                    closeModal();
                    loadTasks();
                } else {
                    const error = await response.json();
                    showToast(error.detail || '保存失败', 'error');
                }
            } catch (error) {
                showToast('保存失败', 'error');
            }
        });

        async function checkNow(taskId) {
            try {
                const response = await fetch(`/api/tasks/${taskId}/check`, { method: 'POST' });
                const result = await response.json();
                if (response.ok) {
                    showToast(result.has_changed ? '检测到变化！' : '内容无变化', 'success');
                    loadTasks();
                } else {
                    showToast(result.detail || '检查失败', 'error');
                }
            } catch (error) {
                showToast('检查失败', 'error');
            }
        }

        async function toggleTask(taskId) {
            try {
                const response = await fetch(`/api/tasks/${taskId}/toggle`, { method: 'POST' });
                if (response.ok) {
                    showToast('操作成功', 'success');
                    loadTasks();
                }
            } catch (error) {
                showToast('操作失败', 'error');
            }
        }

        async function deleteTask(taskId) {
            if (!confirm('确定要删除这个监控任务吗？')) return;
            try {
                const response = await fetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
                if (response.ok) {
                    showToast('删除成功', 'success');
                    loadTasks();
                }
            } catch (error) {
                showToast('删除失败', 'error');
            }
        }

        function showToast(message, type) {
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }

        // 检查配置状态
        async function checkConfig() {
            try {
                const response = await fetch('/api/config');
                const config = await response.json();
                if (!config.smtp_configured) {
                    document.getElementById('configNote').style.display = 'block';
                }
            } catch (error) {
                console.error('检查配置失败:', error);
            }
        }

        // 初始化
        loadTasks();
        checkConfig();
    </script>
</body>
</html>
    """


# ==================== 启动应用 ====================

@app.on_event("startup")
async def startup_event():
    """应用启动时初始化调度器"""
    init_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时停止调度器"""
    scheduler.shutdown()


# ==================== 主程序入口 ====================

def main():
    """主程序入口"""
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║              网页监控告警系统 v1.0.0                           ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  📍 访问地址: http://localhost:8000                           ║
    ║  📖 API文档: http://localhost:8000/docs                       ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  ⚙️  环境变量配置:                                            ║
    ║     SMTP_HOST     - SMTP服务器地址                           ║
    ║     SMTP_PORT     - SMTP端口 (默认587)                       ║
    ║     SMTP_USER     - SMTP用户名/邮箱                          ║
    ║     SMTP_PASSWORD - SMTP密码                                  ║
    ║     SENDER_EMAIL  - 发件人邮箱 (可选)                          ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  💡 使用示例:                                                 ║
    ║     SMTP_HOST=smtp.gmail.com SMTP_USER=user@gmail.com \\     ║
    ║     SMTP_PASSWORD=xxx python main.py                         ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()