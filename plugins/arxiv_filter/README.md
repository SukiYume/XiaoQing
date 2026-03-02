# arXiv Filter 插件

arXiv 论文筛选插件，使用 AI 模型筛选和推荐感兴趣的论文。

## 功能介绍

arXiv Filter 插件可以从 arXiv 每日更新中筛选出符合用户研究兴趣的论文，支持基于关键词、分类和 AI 模型的智能筛选。

## 使用方法

### 基本命令

```
/arxiv today [分类]      # 获取今日论文
/arxiv filter <关键词>    # 按关键词筛选
/arxiv categories         # 列出所有分类
/arxiv help               # 显示帮助信息
```

### 示例

```
/arxiv today astro-ph     # 获取今日天体物理论文
/arxiv filter galaxy      # 筛选包含 galaxy 的论文
/arxiv categories         # 查看所有分类
/arxiv help               # 显示帮助
```

## 配置说明

在 `config/config.json` 中配置：

```json
{
  "plugins": {
    "arxiv_filter": {
      "categories": ["astro-ph", "hep-th", "gr-qc"],
      "keywords": ["galaxy", "cosmology", "dark matter"],
      "use_ai_filter": true,
      "ai_threshold": 0.7,
      "max_results": 10
    }
  }
}
```

### 配置项说明

- `categories` - 关注的 arXiv 分类列表
- `keywords` - 关键词列表
- `use_ai_filter` - 是否使用 AI 模型筛选（默认: false）
- `ai_threshold` - AI 筛选阈值（0-1，默认: 0.7）
- `max_results` - 最大返回结果数（默认: 10）

## 支持的分类

### 物理学
- `astro-ph` - 天体物理
- `hep-th` - 高能物理理论
- `hep-ph` - 高能物理现象学
- `gr-qc` - 广义相对论与量子宇宙学

### 计算机科学
- `cs.AI` - 人工智能
- `cs.LG` - 机器学习
- `cs.CV` - 计算机视觉

完整列表请使用 `/arxiv categories` 查看。

## 功能特性

### 基础筛选
- 按分类筛选
- 按关键词筛选
- 按作者筛选
- 按日期筛选

### AI 智能筛选（可选）
- 使用深度学习模型评估论文相关性
- 基于论文标题和摘要
- 自动排序推荐
- 可调节筛选阈值

### 论文信息
- 标题
- 作者列表
- 摘要
- arXiv ID
- 发布日期
- 链接

## AI 模型

插件包含预训练模型：
- 模型路径: `best_model/`
- 基于论文标题训练
- 分类器类型: 二分类
- 输出: 相关性分数（0-1）

## 数据存储

- 论文数据缓存: `data/papers.json`
- 筛选历史: `data/filter_history.json`
- AI 模型: `best_model/`

## 注意事项

- arXiv API 有速率限制
- AI 筛选需要预训练模型
- 首次运行可能需要下载数据
- 缓存会定期更新

## 依赖

- arxiv (arXiv API 客户端)
- tensorflow/pytorch (如使用 AI 筛选)
- transformers (如使用 AI 筛选)

安装依赖：
```bash
pip install arxiv
# AI 筛选功能需要额外安装
pip install tensorflow transformers
```

## 适用场景

- 科研工作者跟踪领域最新进展
- 筛选感兴趣的研究方向
- 每日论文推送
- 研究方向探索

## 数据来源

- arXiv.org: https://arxiv.org/
- arXiv API: https://arxiv.org/help/api/
