"""处理器初始化"""
from .diary import DiaryHandler
from .event import EventHandler
from .note import NoteHandler
from .search import SearchHandler
from .task import TaskHandler

__all__ = ['EventHandler', 'TaskHandler', 'NoteHandler', 'DiaryHandler', 'SearchHandler']
