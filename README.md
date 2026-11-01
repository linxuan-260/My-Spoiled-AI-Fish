# FISH-AI-OS

> 一个「会过日子」的 QQ 机器人人格系统：五层记忆、只读人格、可逆注入。

**FISH-AI-OS** 不是又一个聊天机器人框架，而是一套**不改变人格、只增强上下文**的记忆中间件。它挂在 AstrBot 的 `on_llm_request` 上，每次调用模型前把「她记得什么、心情怎样、正在干嘛、说话什么样」拼进 system prompt，调用后原样还原。

核心理念：**人格是只读的**。所有增强都是带 marker 的临时注入块，拿掉 marker 后 prompt 与注入前逐字节相同。

---

## 特性

- **五层记忆**：记忆 / 情绪 / 生活 / 表达 / 反馈，各自独立打分为预算
- **人格只读**：从不写回 `personas` 表，可逆、可审计
- **跨会话记忆**：主人在私聊里说过的事，群里也记得
- **共享层**：群里的事可以定向开放给指定会话，陌生人读不到
- **自主探索**：每两小时自己搜个好奇的问题、读一段正文、用她自己的话记下来
- **记忆星图**：把记忆的归属、共注入、语义关系画成一张图
- **注入账本**：每次注入的层、命中的记忆 id、字符数全部落库，事后可查

---

## 快速开始

```bash
# 1. 放插件
cp -r plugins/astrbot_plugin_fish_memory /opt/astrbot/data/plugins/
cp -r plugins/astrbot_plugin_qzone        /opt/astrbot/data/plugins/

# 2. 配身份（必改）
cp config/fish_memory.config.example.json \
   /opt/astrbot/data/config/astrbot_plugin_fish_memory_config.json
# 编辑 master.owner_id -> 你自己的 QQ

# 3. 起 worker 与面板
systemctl enable --now fishmem-workers fishmem-panel
systemctl restart astrbot
```

完整步骤见 **[docs/安装.md](docs/安装.md)**。

---

## 身份绑定规则 ⭐

这是整个系统最容易配错、后果也最严重的一块。搞错了要么「她记不住你说的话」，要么「陌生人能读到群里的内容」。

### 三个 QQ 号，别搞混

| 角色 | 是什么 | 填在哪 |
|---|---|---|
| **机器人 QQ** | 跑着 OneBot 的那个号 | 不填，从会话的 `umo` 里自动解析 |
| **主人 QQ** | 你自己 | `master.owner_id`，字符串 |
| **群号** | 需要共享的那个群 | 写进 `public_scopes` 的会话字符串里 |

### 会话标识（umo）长什么样

```
aiocqhttp-<机器人QQ>:GroupMessage:<群号>
aiocqhttp-<机器人QQ>:FriendMessage:<对方QQ>
```

`public_scopes` 和 `public_readers` 里填的就是这种整串。

### 可见性三层

| 值 | 谁看得到 | 什么时候自动打上 |
|---|---|---|
| `private` | 只有本会话 | 默认 |
| `owner` | 本会话 + 主人的**其它任何会话** | 一批消息只来自 `owner_id` |
| `public` | 共享层读者 | 手动公开，或 `auto_share_categories` 命中 |

同一条内容的哈希撞车时，可见性**只升不降** —— 不会因为一次误判把已共享的记忆降回私有。

### 典型配置：群里的事，私聊里也想得起来

```json
{
  "master": {
    "owner_id": "123456789",
    "public_scopes":  ["aiocqhttp-<机器人QQ>:GroupMessage:<群号>"],
    "public_readers": ["aiocqhttp-<机器人QQ>:FriendMessage:123456789"]
  }
}
```

含义：**来源**是那个群，**读者**是主人私聊。`public_readers` 留空 = 所有会话都能读来源 —— 除非你确定，否则不要留空。

### 怎么验证绑定生效了

```bash
# 私聊她问一件群里说过的事，然后看这次注入命中了哪些记忆
/opt/astrbot/.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/astrbot/data/plugin_data/astrbot_plugin_fish_memory/memory/memory.db')
for row in c.execute("select ts,layer,memory_ids from injection_ledger order by id desc limit 5"):
    print(row)
PY
```

如果私聊场景下 `layer='memory'` 的 `memory_ids` 里出现了群里的记忆 id，说明共享层通了。

反过来，用**陌生号**私聊她，同样的查询应该一条 `memory` 都没有。

### 常见配错的症状

| 症状 | 原因 |
|---|---|
| 私聊里她不记得你刚才说的 | `owner_id` 没填或填错 |
| 陌生人能聊到群里的内容 | `public_readers` 留空（=对所有会话开放） |
| 群里的事私聊问不出来 | `public_scopes` 里的 umo 拼错了，注意中间是 `-` 和 `:` |
| 记忆全挤在一个会话下 | 入库批次跨会话混在一起（本仓库已按 `scope_id` 分组修复） |

---

## 群聊防禁言指南 ⭐

被禁言基本只有两种原因：**平台风控**（发得太勤）和**群管不满**（刷屏、乱跑自动行为）。

**三条硬规则：**

1. **`trigger.read_prob` 保持 0.001 或更小。** 它决定「每来一条消息就随机去翻对方空间」的概率，调大等于让机器人到处翻别人说说。
2. **说说的两条间隔 ≥ 3 小时、每天 ≤ 6 条。** 这层闸在代码里（`service.publish_post`），改配置绕不过去。
3. **定时任务必须带抖动。** `publish_offset = 600` 让实际执行散在 20 分钟窗口里；设成 0 就成了「每天 23:30:00 整」的机器人签名。

另外：`send_read_card` **保持 `false`**。开了以后，读到的好友说说不论是否「好友可见」都会被渲染成卡片发出去 —— 这是隐私事故，也是被投诉的高发点。

完整清单（含忽略名单、权限边界、被禁言后的处置）见 **[docs/防禁言.md](docs/防禁言.md)**。

---

## 目录结构

```
plugins/
  astrbot_plugin_fish_memory/   五层记忆插件（本项目主体，MIT）
  astrbot_plugin_qzone/         QQ空间插件（上游第三方，GPL-3.0）
tools/
  fishmem_web.py                五层记忆面板 + /api/graph
  fishmem_web_ui.html           面板前端
  fishmem_starmap.html          记忆星图
  fishmem_workers.sh            ingest / life / digest 三进程管理
docs/
  安装.md  架构.md  配置.md  防禁言.md
config/
  fish_memory.config.example.json
scripts/
  sync-from-server.sh           从服务器拉代码（本地开发用）
```

## 文档

| 文档 | 内容 |
|---|---|
| [安装](docs/安装.md) | 环境、systemd、首次验证 |
| [架构](docs/架构.md) | 五层模型、注入流水线、三个必须知道的历史坑 |
| [配置](docs/配置.md) | 全部关键配置项与热重载边界 |
| [防禁言](docs/防禁言.md) | 频率、节流、隐私外发、处置流程 |
| [插件清单](docs/插件清单.md) | 本机装过哪些插件及版本 |

## 数据与隐私

运行时数据全部在 `data/plugin_data/` 下，**不进仓库**：

- `memory.db` —— 记忆内容和注入账本（**含聊天记录**）
- `emotion.db` / `life.db` / `style.db` —— 情绪、生活、表达
- 面板和星图都在 `127.0.0.1` 上，对外请走 SSH 隧道

仓库里不包含任何真实 QQ 号、服务器地址或 API Key。所有示例都用 `123456789` 这类占位值。

## 许可证

- 本项目自身代码：**MIT**
- `plugins/astrbot_plugin_qzone`：**GPL-3.0**（上游 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的修改版，遵循原许可证）

详见 [LICENSES.md](LICENSES.md)。

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件宿主与消息管线
- [Zhalslar](https://github.com/Zhalslar) —— qzone 插件上游
