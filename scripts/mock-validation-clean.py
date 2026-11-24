#!/usr/bin/env python3
"""清理联调假数据（mock 模式产物）: validation-mock-* 任务/步骤 + d-mock-* 合成告警事件。
只删 mock 前缀数据, 真实验证任务与真实告警不受影响。"""
import sqlite3

DB = '/opt/artifacts/core-drill/platform.db'
c = sqlite3.connect(DB)
with c:
    steps = c.execute("DELETE FROM validation_steps WHERE task_id LIKE 'validation-mock-%'").rowcount
    tasks = c.execute("DELETE FROM validation_tasks WHERE task_id LIKE 'validation-mock-%'").rowcount
    events = c.execute("DELETE FROM events WHERE drill_id LIKE 'd-mock-%'").rowcount
print(f'已清: 任务 {tasks} / 步骤 {steps} / 合成告警 {events}')
