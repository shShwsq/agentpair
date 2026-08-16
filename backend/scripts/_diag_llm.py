"""一次性诊断脚本:核对出题模型解析实际数据(只读,不打印 api_key)

查询:
1. 目标任务的 llm_config_id / react_llm_config_id
2. 该用户的 practice_settings.default_llm_config_id
3. 该用户 UserLLMConfig 配置列表(id→name/provider/model)
"""
from app.database import SessionLocal
import app.models.task_artifact  # noqa: F401  让 Task mapper 解析 TaskArtifact 关联
from app.models.practice import PracticeSettings
from app.models.task import Task
from app.models.user_llm_config import UserLLMConfig

TASK_ID = "e5bddbb2-6ea4-456d-8289-3001e4d8b410"
USER_ID = "b160111c-852c-44e8-8759-4c6421b16f79"

db = SessionLocal()
try:
    task = db.query(Task).filter(Task.id == TASK_ID).first()
    print("== Task ==")
    if task:
        print("task_id      :", task.id)
        print("user_id      :", task.user_id)
        print("llm_config_id:", task.llm_config_id)
        print("react_llm_config_id:", task.react_llm_config_id)
    else:
        print("任务不存在")

    pref = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == USER_ID
    ).first()
    print("\n== PracticeSettings ==")
    if pref:
        print("default_llm_config_id:", pref.default_llm_config_id)
    else:
        print("无 practice_settings 行")

    cfg_row = db.query(UserLLMConfig).filter(
        UserLLMConfig.user_id == USER_ID
    ).first()
    print("\n== UserLLMConfig ==")
    if cfg_row:
        for c in (cfg_row.llm_configs or []):
            print(
                "  id=%s name=%s provider=%s model=%s thinking=%s",
                c.get("id"), c.get("name"), c.get("provider"),
                c.get("model"), c.get("enable_thinking"),
            )
    else:
        print("无 user_llm_configs 行")
finally:
    db.close()
