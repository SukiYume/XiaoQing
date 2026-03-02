"""
常量定义（精简版）
仅保留数据模型相关的字段常量
"""


class ItemFields:
    """数据库字段名常量"""
    # 通用字段
    ID = 'id'
    TYPE = 'type'
    TITLE = 'title'
    CONTENT = 'content'
    TAGS = 'tags'
    CATEGORY = 'category'
    CREATED_AT = 'created_at'
    UPDATED_AT = 'updated_at'
    OWNER_ID = 'owner_id'
    CONTEXT = 'context'
    VISIBILITY = 'visibility'
    ATTACHMENTS = 'attachments'
    AI_META = 'ai_meta'
    DELETED = 'deleted'
    
    # Event 扩展字段
    START_TIME = 'start_time'
    END_TIME = 'end_time'
    TIMEZONE = 'timezone'
    LOCATION = 'location'
    PARTICIPANTS = 'participants'
    RRULE = 'rrule'
    REMIND_POLICY_ID = 'remind_policy_id'
    
    # Task 扩展字段
    DUE_TIME = 'due_time'
    PRIORITY = 'priority'
    STATUS = 'status'
    ESTIMATE = 'estimate'
    SUBTASKS = 'subtasks'
    DEPENDENCY = 'dependency'
    PROGRESS = 'progress'
    
    # Diary 扩展字段
    DIARY_DATE = 'diary_date'
    MOOD = 'mood'
    WEATHER = 'weather'
    TEMPLATE_ID = 'template_id'
