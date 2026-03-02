"""
Jupyter 插件数据模型
"""
from dataclasses import dataclass, field
from pathlib import Path

from .jupyter_config import MAX_OUTPUT_LENGTH

@dataclass
class ExecutionResult:
    """代码执行结果"""
    success: bool = True
    stdout: str = ""
    stderr: str = ""
    result: str = ""
    images: list[Path] = field(default_factory=list)
    error: str = ""
    execution_time: float = 0.0
    
    def format_output(self) -> str:
        """格式化为可读文本"""
        parts = []
        
        if self.stdout:
            parts.append(self.stdout.strip())
        
        if self.result and self.result != "None":
            parts.append(f">>> {self.result}")
        
        if self.stderr:
            parts.append(f"⚠️ {self.stderr.strip()}")
        
        if self.error:
            parts.append(f"❌ {self.error}")
        
        if self.execution_time > 0:
            parts.append(f"\n⏱️ {self.execution_time:.2f}s")
        
        output = "\n".join(parts) if parts else "(无输出)"
        
        # 截断过长输出
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"
        
        return output
