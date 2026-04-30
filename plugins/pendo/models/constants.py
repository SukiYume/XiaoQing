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
    
    # Task 扩展字段
    PLAN_DATE = 'plan_date'
    DEADLINE_AT = 'deadline_at'
    PRIORITY = 'priority'
    STATUS = 'status'
    REPEAT_RULE = 'repeat_rule'
    COMPLETED_AT = 'completed_at'
    CANCELLED_AT = 'cancelled_at'
    
    # Diary 扩展字段
    DIARY_DATE = 'diary_date'
    ENTRY_TIME = 'entry_time'
    MOOD = 'mood'
    MOOD_SCORE = 'mood_score'
    WEATHER = 'weather'
    TEMPLATE_ID = 'template_id'
    TEMPLATE_ANSWERS = 'template_answers'
    IS_FAVORITE = 'is_favorite'

    # Ledger 扩展字段
    AMOUNT = 'amount'
    AMOUNT_CENTS = 'amount_cents'
    CURRENCY = 'currency'
    TRANSACTION_TYPE = 'transaction_type'
    LEDGER_CATEGORY = 'ledger_category'
    LEDGER_DATE = 'ledger_date'
    ACCOUNT_NAME = 'account_name'
    COUNTER_ACCOUNT_NAME = 'counter_account_name'
    MERCHANT = 'merchant'
    REMARK = 'remark'
