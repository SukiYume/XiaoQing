"""处理器初始化"""
from .event import EventHandler
from .task import TaskHandler
from .note import NoteHandler
from .diary import DiaryHandler
from .search import SearchHandler

__all__ = ['EventHandler', 'TaskHandler', 'NoteHandler', 'DiaryHandler', 'SearchHandler']
